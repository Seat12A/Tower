from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain import error_codes as ec
from tower.core.domain.cases import (
    INITIAL_STATUS,
    TRANSITION_PERMISSION,
    CaseKind,
    CaseStatus,
    check_transition,
    parse_publication_url,
)
from tower.core.domain.errors import Conflict, InvalidInput, NotFound
from tower.core.domain.security import Actor, Permission
from tower.core.events.outbox import publish
from tower.core.models.cases import Case, CaseEvent

AGGREGATE = "case"


def case_payload(d: Case) -> dict[str, Any]:
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
        "discord_thread_id": None if d.discord_thread_id is None else str(d.discord_thread_id),
        "version": d.version,
    }


async def open_case(
    session: AsyncSession,
    actor: Actor,
    *,
    kind: CaseKind,
    x_post_id: int | None = None,
    title: str | None = None,
    priority: int = 0,
    reason: str | None = None,
) -> Case:
    actor.require(Permission.CASE_CREATE)
    if not -100 <= priority <= 100:
        raise InvalidInput("Priority out of range (-100..100)", code=ec.DOS_BAD_PRIORITY)
    case = Case(
        kind=kind.value,
        status=INITIAL_STATUS.value,
        x_post_id=x_post_id,
        title=title,
        priority=priority,
        opened_by=actor.label,
    )
    session.add(case)
    await session.flush()
    await session.refresh(case)
    session.add(
        CaseEvent(
            case_id=case.id,
            from_status=None,
            to_status=case.status,
            actor=actor.label,
            reason=reason,
        )
    )
    await publish(
        session,
        "case.opened",
        case_payload(case),
        aggregate_type=AGGREGATE,
        aggregate_id=str(case.public_id),
        actor=actor.label,
    )
    return case


async def get_case(session: AsyncSession, actor: Actor, public_id: uuid.UUID) -> Case:
    case = await session.scalar(select(Case).where(Case.public_id == public_id))
    if case is None or not _can_see(actor, case):
        raise NotFound("Case not found", code=ec.DOS_NOT_FOUND, public_id=str(public_id))
    return case


async def link_discord_thread(
    session: AsyncSession, actor: Actor, public_id: uuid.UUID, thread_id: int
) -> None:
    """Record the Discord forum post showing the case. Operational marker, like
    ``mark_draft_notified``: no audit, no event, no version bump."""
    actor.require(Permission.CASE_READ)
    if not 0 < thread_id < 2**63:
        raise InvalidInput("Invalid Discord thread id", code=ec.DOS_BAD_THREAD_ID)
    result = await session.execute(
        text("UPDATE cases SET discord_thread_id = :t WHERE public_id = :p"),
        {"t": thread_id, "p": public_id},
    )
    if result.rowcount == 0:  # type: ignore[attr-defined]
        raise NotFound("Case not found", code=ec.DOS_NOT_FOUND, public_id=str(public_id))


async def list_cases(
    session: AsyncSession,
    actor: Actor,
    *,
    status: CaseStatus | None = None,
    kind: CaseKind | None = None,
    limit: int = 50,
    before_id: int | None = None,
) -> Sequence[Case]:
    stmt = select(Case).order_by(Case.id.desc()).limit(min(max(limit, 1), 200))
    if not actor.can(Permission.CASE_READ):
        actor.require(Permission.CASE_READ_PUBLIC)
        stmt = stmt.where(Case.status == CaseStatus.PUBLISHED.value)
    if status is not None:
        stmt = stmt.where(Case.status == status.value)
    if kind is not None:
        stmt = stmt.where(Case.kind == kind.value)
    if before_id is not None:
        stmt = stmt.where(Case.id < before_id)
    return (await session.scalars(stmt)).all()


async def transition(
    session: AsyncSession,
    actor: Actor,
    public_id: uuid.UUID,
    target: CaseStatus,
    *,
    reason: str | None = None,
    expected_version: int | None = None,
    published_url: str | None = None,
) -> Case:
    """Advance a case. Locks the row so two reviewers cannot race."""
    actor.require(TRANSITION_PERMISSION[target])
    case = await session.scalar(select(Case).where(Case.public_id == public_id).with_for_update())
    if case is None or not _can_see(actor, case):
        raise NotFound("Case not found", code=ec.DOS_NOT_FOUND, public_id=str(public_id))
    if expected_version is not None and case.version != expected_version:
        raise Conflict(
            "The case was modified in the meantime",
            expected=expected_version,
            current=case.version,
        )
    kind = CaseKind(case.kind)
    current = CaseStatus(case.status)
    check_transition(kind, current, target)

    data: dict[str, Any] = {}
    if target is CaseStatus.IN_REVIEW and actor.discord_id is not None:
        case.assigned_to = actor.discord_id
    if target is CaseStatus.PUBLISHED:
        if not published_url:
            raise InvalidInput(
                "The X publication link is required",
                code=ec.DOS_PUBLICATION_URL_REQUIRED,
            )
        data["publication_post_id"] = str(parse_publication_url(published_url))
        case.published_url = published_url.strip()
    elif published_url:
        raise InvalidInput(
            "published_url is only accepted for publication",
            code=ec.DOS_PUBLICATION_URL_UNEXPECTED,
        )
    case.status = target.value
    case.updated_at = func.now()
    await session.flush()
    await session.refresh(case)

    session.add(
        CaseEvent(
            case_id=case.id,
            from_status=current.value,
            to_status=target.value,
            actor=actor.label,
            reason=reason,
            data=data,
        )
    )
    await publish(
        session,
        "case.transitioned",
        {**case_payload(case), "from": current.value, "to": target.value, "reason": reason},
        aggregate_type=AGGREGATE,
        aggregate_id=str(case.public_id),
        actor=actor.label,
    )
    return case


async def mark_published(
    session: AsyncSession,
    actor: Actor,
    public_id: uuid.UUID,
    url: str,
    *,
    expected_version: int | None = None,
) -> Case:
    """Mark as published: the team posted on X and pastes the link."""
    return await transition(
        session,
        actor,
        public_id,
        CaseStatus.PUBLISHED,
        published_url=url,
        expected_version=expected_version,
    )


async def history(session: AsyncSession, actor: Actor, public_id: uuid.UUID) -> Sequence[CaseEvent]:
    actor.require(Permission.CASE_READ)
    case = await get_case(session, actor, public_id)
    stmt = select(CaseEvent).where(CaseEvent.case_id == case.id).order_by(CaseEvent.id)
    return (await session.scalars(stmt)).all()


def _can_see(actor: Actor, case: Case) -> bool:
    if actor.can(Permission.CASE_READ):
        return True
    return actor.can(Permission.CASE_READ_PUBLIC) and case.status == CaseStatus.PUBLISHED
