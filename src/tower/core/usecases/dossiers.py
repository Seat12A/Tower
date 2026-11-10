from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain import error_codes as ec
from tower.core.domain.dossiers import (
    INITIAL_STATUS,
    TRANSITION_PERMISSION,
    DossierKind,
    DossierStatus,
    check_transition,
    parse_publication_url,
)
from tower.core.domain.errors import Conflict, InvalidInput, NotFound
from tower.core.domain.security import Actor, Permission
from tower.core.events.outbox import publish
from tower.core.models.dossiers import Dossier, DossierEvent

AGGREGATE = "dossier"


def dossier_payload(d: Dossier) -> dict[str, Any]:
    """Representation published in events (X IDs as strings)."""
    return {
        "public_id": str(d.public_id),
        "kind": d.kind,
        "status": d.status,
        "x_post_id": None if d.x_post_id is None else str(d.x_post_id),
        "title": d.title,
        "priority": d.priority,
        "assigned_to": None if d.assigned_to is None else str(d.assigned_to),
        "published_url": d.published_url,
        "version": d.version,
    }


async def open_dossier(
    session: AsyncSession,
    actor: Actor,
    *,
    kind: DossierKind,
    x_post_id: int | None = None,
    title: str | None = None,
    priority: int = 0,
    reason: str | None = None,
) -> Dossier:
    actor.require(Permission.DOSSIER_CREATE)
    if not -100 <= priority <= 100:
        raise InvalidInput("Priority out of range (-100..100)", code=ec.DOS_BAD_PRIORITY)
    dossier = Dossier(
        kind=kind.value,
        status=INITIAL_STATUS.value,
        x_post_id=x_post_id,
        title=title,
        priority=priority,
        opened_by=actor.label,
    )
    session.add(dossier)
    await session.flush()
    await session.refresh(dossier)
    session.add(
        DossierEvent(
            dossier_id=dossier.id,
            from_status=None,
            to_status=dossier.status,
            actor=actor.label,
            reason=reason,
        )
    )
    await publish(
        session,
        "dossier.opened",
        dossier_payload(dossier),
        aggregate_type=AGGREGATE,
        aggregate_id=str(dossier.public_id),
        actor=actor.label,
    )
    return dossier


async def get_dossier(session: AsyncSession, actor: Actor, public_id: uuid.UUID) -> Dossier:
    dossier = await session.scalar(select(Dossier).where(Dossier.public_id == public_id))
    if dossier is None or not _can_see(actor, dossier):
        raise NotFound("Dossier not found", code=ec.DOS_NOT_FOUND, public_id=str(public_id))
    return dossier


async def list_dossiers(
    session: AsyncSession,
    actor: Actor,
    *,
    status: DossierStatus | None = None,
    kind: DossierKind | None = None,
    limit: int = 50,
    before_id: int | None = None,
) -> Sequence[Dossier]:
    stmt = select(Dossier).order_by(Dossier.id.desc()).limit(min(max(limit, 1), 200))
    if not actor.can(Permission.DOSSIER_READ):
        actor.require(Permission.DOSSIER_READ_PUBLIC)
        stmt = stmt.where(Dossier.status == DossierStatus.PUBLISHED.value)
    if status is not None:
        stmt = stmt.where(Dossier.status == status.value)
    if kind is not None:
        stmt = stmt.where(Dossier.kind == kind.value)
    if before_id is not None:
        stmt = stmt.where(Dossier.id < before_id)
    return (await session.scalars(stmt)).all()


async def transition(
    session: AsyncSession,
    actor: Actor,
    public_id: uuid.UUID,
    target: DossierStatus,
    *,
    reason: str | None = None,
    expected_version: int | None = None,
    published_url: str | None = None,
) -> Dossier:
    """Advance a dossier. Locks the row so two reviewers cannot race."""
    actor.require(TRANSITION_PERMISSION[target])
    dossier = await session.scalar(
        select(Dossier).where(Dossier.public_id == public_id).with_for_update()
    )
    if dossier is None or not _can_see(actor, dossier):
        raise NotFound("Dossier not found", code=ec.DOS_NOT_FOUND, public_id=str(public_id))
    if expected_version is not None and dossier.version != expected_version:
        raise Conflict(
            "The dossier was modified in the meantime",
            expected=expected_version,
            current=dossier.version,
        )
    kind = DossierKind(dossier.kind)
    current = DossierStatus(dossier.status)
    check_transition(kind, current, target)

    data: dict[str, Any] = {}
    if target is DossierStatus.IN_REVIEW and actor.discord_id is not None:
        dossier.assigned_to = actor.discord_id
    if target is DossierStatus.PUBLISHED:
        if not published_url:
            raise InvalidInput(
                "The X publication link is required",
                code=ec.DOS_PUBLICATION_URL_REQUIRED,
            )
        data["publication_post_id"] = str(parse_publication_url(published_url))
        dossier.published_url = published_url.strip()
    elif published_url:
        raise InvalidInput(
            "published_url is only accepted for publication",
            code=ec.DOS_PUBLICATION_URL_UNEXPECTED,
        )
    dossier.status = target.value
    dossier.updated_at = func.now()
    await session.flush()
    await session.refresh(dossier)

    session.add(
        DossierEvent(
            dossier_id=dossier.id,
            from_status=current.value,
            to_status=target.value,
            actor=actor.label,
            reason=reason,
            data=data,
        )
    )
    await publish(
        session,
        "dossier.transitioned",
        {**dossier_payload(dossier), "from": current.value, "to": target.value, "reason": reason},
        aggregate_type=AGGREGATE,
        aggregate_id=str(dossier.public_id),
        actor=actor.label,
    )
    return dossier


async def mark_published(
    session: AsyncSession,
    actor: Actor,
    public_id: uuid.UUID,
    url: str,
    *,
    expected_version: int | None = None,
) -> Dossier:
    """Mark as published: the team posted on X and pastes the link."""
    return await transition(
        session,
        actor,
        public_id,
        DossierStatus.PUBLISHED,
        published_url=url,
        expected_version=expected_version,
    )


async def history(
    session: AsyncSession, actor: Actor, public_id: uuid.UUID
) -> Sequence[DossierEvent]:
    actor.require(Permission.DOSSIER_READ)
    dossier = await get_dossier(session, actor, public_id)
    stmt = (
        select(DossierEvent).where(DossierEvent.dossier_id == dossier.id).order_by(DossierEvent.id)
    )
    return (await session.scalars(stmt)).all()


def _can_see(actor: Actor, dossier: Dossier) -> bool:
    if actor.can(Permission.DOSSIER_READ):
        return True
    return actor.can(Permission.DOSSIER_READ_PUBLIC) and dossier.status == DossierStatus.PUBLISHED
