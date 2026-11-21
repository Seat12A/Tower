"""Affiliations between individual accounts and badged organisations.

Rules (docs/badges.md):
- the organisation must hold an active organisation, media or institutional badge;
- an affiliation becomes active only once validated by the organisation (organisation
  key) or by the team; it expires one year after its last validation unless reconfirmed;
- one primary affiliation per member (the one displayed); the next active one is promoted
  when it ends;
- when the organisation loses its badge (or an affiliable category), all its
  affiliations end.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from tower.core.domain import error_codes as ec
from tower.core.domain.badges import (
    LIVE_AFFILIATION_STATUSES,
    MAX_TITLE,
    AccountRef,
    AffiliationSource,
    AffiliationStatus,
    BadgeAction,
    BadgeStatus,
    affiliation_expiry,
    check_text,
    normalize_category,
)
from tower.core.domain.errors import Conflict, InvalidInput, NotFound, PermissionDenied
from tower.core.domain.security import Actor, ActorKind, Permission
from tower.core.events.outbox import publish
from tower.core.models.badges import Badge, BadgeAffiliation
from tower.core.models.ingestion import PlatformAccount
from tower.core.usecases.badges import (
    account_ref_payload,
    active_org_badge,
    ensure_account,
    is_affiliable,
    org_badge_of_actor,
    record,
    require_any,
    required,
    utcnow,
)

AGGREGATE = "badge_affiliation"


def affiliation_payload(
    a: BadgeAffiliation, member: PlatformAccount, org: Badge, org_account: PlatformAccount
) -> dict[str, Any]:
    return {
        "public_id": str(a.public_id),
        "member": {**account_ref_payload(member), "handle": member.handle},
        "org": {
            **account_ref_payload(org_account),
            "badge": str(org.public_id),
            "label": org.label,
            "category": normalize_category(org.category),
        },
        "status": a.status,
        "primary": a.is_primary,
        "title": a.title,
        "source": a.source,
        "wikidata_property": a.wikidata_property,
        "requested_by": a.requested_by,
        "validated_by": a.validated_by,
        "validated_at": a.validated_at.isoformat() if a.validated_at else None,
        "expires_at": a.expires_at.isoformat() if a.expires_at else None,
        "ended_by": a.ended_by,
        "ended_at": a.ended_at.isoformat() if a.ended_at else None,
        "end_reason": a.end_reason,
        "version": a.version,
    }


async def _publish(
    session: AsyncSession, actor: Actor, action: BadgeAction, a: BadgeAffiliation
) -> None:
    member, org, org_account = await _parts(session, a)
    await publish(
        session,
        "badge.affiliation_changed",
        {"action": action.value, "affiliation": affiliation_payload(a, member, org, org_account)},
        aggregate_type=AGGREGATE,
        aggregate_id=str(a.public_id),
        actor=actor.label,
    )


async def _parts(
    session: AsyncSession, a: BadgeAffiliation
) -> tuple[PlatformAccount, Badge, PlatformAccount]:
    member = required(await session.get(PlatformAccount, a.member_account_id), "member")
    org = required(await session.get(Badge, a.org_badge_id), "organisation badge")
    org_account = required(await session.get(PlatformAccount, org.account_id), "org account")
    return member, org, org_account


def _require_manager(actor: Actor, org: Badge) -> None:
    """Team (``badges:manage``) or the organisation itself (its own affiliations)."""
    if actor.can(Permission.BADGES_MANAGE):
        return
    if actor.kind is ActorKind.ORGANIZATION and actor.can(Permission.BADGES_ORG_MANAGE):
        if actor.id == str(org.public_id):
            return
        raise PermissionDenied(
            "Affiliation belongs to another organisation", code=ec.BDG_NOT_YOUR_AFFILIATION
        )
    actor.require(Permission.BADGES_MANAGE)


async def _member_blocked(session: AsyncSession, account_id: int) -> bool:
    status = await session.scalar(select(Badge.status).where(Badge.account_id == account_id))
    return status == BadgeStatus.BLOCKED.value


async def create_affiliation(
    session: AsyncSession,
    actor: Actor,
    member_ref: AccountRef,
    *,
    org_ref: AccountRef | None = None,
    title: str | None = None,
    validate: bool = True,
    source: AffiliationSource | None = None,
    handle: str | None = None,
) -> BadgeAffiliation:
    """Create (or reopen an ended) affiliation.

    - team (``badges:manage``): ``org_ref`` required; ``validate`` makes it active at once
      (validated by the team), otherwise it waits as ``pending``;
    - organisation actor: ``org_ref`` is its own account (or omitted); always active
      (validated by the organisation).
    """
    require_any(actor, Permission.BADGES_MANAGE, Permission.BADGES_ORG_MANAGE)
    title = check_text(title, field="title", max_chars=MAX_TITLE)
    if actor.can(Permission.BADGES_MANAGE):
        if org_ref is None:
            raise InvalidInput("org is required", code=ec.BDG_NOT_AFFILIABLE)
        org, org_account = await active_org_badge(session, org_ref, lock=True)
        src = source or AffiliationSource.MANUAL
    else:
        org, org_account = await org_badge_of_actor(session, actor)
        if org_ref is not None and (
            org_ref.platform.value != org_account.platform
            or org_ref.external_id != org_account.external_id
        ):
            raise PermissionDenied(
                "Affiliation belongs to another organisation", code=ec.BDG_NOT_YOUR_AFFILIATION
            )
        src = AffiliationSource.ORGANIZATION
        validate = True
    member = await ensure_account(session, member_ref, handle=handle)
    if member.id == org_account.id:
        raise InvalidInput(
            "An account cannot be affiliated with itself", code=ec.BDG_SELF_AFFILIATION
        )
    if await _member_blocked(session, member.id):
        raise Conflict("Account is blocked from badges", code=ec.BDG_ACCOUNT_BLOCKED)
    existing = await session.scalar(
        select(BadgeAffiliation)
        .where(
            BadgeAffiliation.member_account_id == member.id,
            BadgeAffiliation.org_badge_id == org.id,
        )
        .with_for_update()
    )
    if existing is not None and AffiliationStatus(existing.status) in LIVE_AFFILIATION_STATUSES:
        raise Conflict(
            "Affiliation already pending or active",
            code=ec.BDG_AFFILIATION_EXISTS,
            affiliation=str(existing.public_id),
            status=existing.status,
        )
    now = utcnow()
    a = existing or BadgeAffiliation(member_account_id=member.id, org_badge_id=org.id)
    a.status = AffiliationStatus.PENDING.value
    a.title = title
    a.source = src.value
    a.wikidata_property = None
    a.requested_by = actor.label
    a.validated_by = a.validated_at = a.expires_at = None
    a.ended_by = a.ended_at = a.end_reason = None
    a.is_primary = False
    if existing is None:
        session.add(a)
    else:
        a.version += 1
        a.updated_at = now
    await session.flush()
    await session.refresh(a)
    await record(
        session,
        account_id=member.id,
        affiliation_id=a.id,
        action=BadgeAction.AFFILIATION_CREATED,
        actor=actor,
        source=src.value,
        changes={"org": str(org.public_id), "title": title},
    )
    await _publish(session, actor, BadgeAction.AFFILIATION_CREATED, a)
    if validate:
        await _activate(session, actor, a, BadgeAction.AFFILIATION_VALIDATED, now)
    return a


async def _get(session: AsyncSession, public_id: str) -> BadgeAffiliation:
    try:
        pid = uuid.UUID(public_id)
    except ValueError:
        raise NotFound("Affiliation not found", code=ec.BDG_AFFILIATION_NOT_FOUND) from None
    a = await session.scalar(
        select(BadgeAffiliation).where(BadgeAffiliation.public_id == pid).with_for_update()
    )
    if a is None:
        raise NotFound(
            "Affiliation not found", code=ec.BDG_AFFILIATION_NOT_FOUND, public_id=str(pid)
        )
    return a


def _transition_error(a: BadgeAffiliation, target: str) -> Conflict:
    return Conflict(
        "Invalid affiliation transition",
        code=ec.BDG_AFFILIATION_TRANSITION,
        status=a.status,
        target=target,
    )


async def _activate(
    session: AsyncSession, actor: Actor, a: BadgeAffiliation, action: BadgeAction, now: datetime
) -> None:
    await _lock_member(session, a.member_account_id)
    a.status = AffiliationStatus.ACTIVE.value
    a.validated_by = actor.label
    a.validated_at = now
    a.expires_at = affiliation_expiry(now)
    a.ended_by = a.ended_at = a.end_reason = None
    a.version += 1
    a.updated_at = now
    await session.flush()
    has_primary = await session.scalar(
        select(BadgeAffiliation.id).where(
            BadgeAffiliation.member_account_id == a.member_account_id,
            BadgeAffiliation.is_primary,
        )
    )
    if has_primary is None:
        a.is_primary = True
        await session.flush()
    await record(
        session,
        account_id=a.member_account_id,
        affiliation_id=a.id,
        action=action,
        actor=actor,
        changes={"expires_at": a.expires_at.isoformat(), "primary": a.is_primary},
    )
    await _publish(session, actor, action, a)


async def validate_affiliation(
    session: AsyncSession, actor: Actor, public_id: str
) -> BadgeAffiliation:
    """``pending`` or ``expired`` → ``active`` (team or the organisation)."""
    require_any(actor, Permission.BADGES_MANAGE, Permission.BADGES_ORG_MANAGE)
    a = await _get(session, public_id)
    org = await _org_for(session, a, actor)
    if a.status not in (AffiliationStatus.PENDING.value, AffiliationStatus.EXPIRED.value):
        raise _transition_error(a, "active")
    if not is_affiliable(org):
        raise Conflict("Organisation has no affiliable badge", code=ec.BDG_NOT_AFFILIABLE)
    if await _member_blocked(session, a.member_account_id):
        raise Conflict("Account is blocked from badges", code=ec.BDG_ACCOUNT_BLOCKED)
    await _activate(session, actor, a, BadgeAction.AFFILIATION_VALIDATED, utcnow())
    return a


async def reconfirm_affiliation(
    session: AsyncSession, actor: Actor, public_id: str
) -> BadgeAffiliation:
    """Renew an active affiliation for one more year (team or the organisation)."""
    require_any(actor, Permission.BADGES_MANAGE, Permission.BADGES_ORG_MANAGE)
    a = await _get(session, public_id)
    await _org_for(session, a, actor)
    if a.status != AffiliationStatus.ACTIVE.value:
        raise _transition_error(a, "active")
    now = utcnow()
    a.validated_by = actor.label
    a.validated_at = now
    a.expires_at = affiliation_expiry(now)
    a.version += 1
    a.updated_at = now
    await session.flush()
    await record(
        session,
        account_id=a.member_account_id,
        affiliation_id=a.id,
        action=BadgeAction.AFFILIATION_RECONFIRMED,
        actor=actor,
        changes={"expires_at": a.expires_at.isoformat()},
    )
    await _publish(session, actor, BadgeAction.AFFILIATION_RECONFIRMED, a)
    return a


async def reject_affiliation(
    session: AsyncSession, actor: Actor, public_id: str, *, reason: str | None = None
) -> BadgeAffiliation:
    """``pending`` → ``rejected``. A rejected Wikidata link is never proposed again."""
    require_any(actor, Permission.BADGES_MANAGE, Permission.BADGES_ORG_MANAGE)
    a = await _get(session, public_id)
    await _org_for(session, a, actor)
    if a.status != AffiliationStatus.PENDING.value:
        raise _transition_error(a, "rejected")
    await _end(session, actor, a, AffiliationStatus.REJECTED, reason)
    return a


async def revoke_affiliation(
    session: AsyncSession, actor: Actor, public_id: str, *, reason: str | None = None
) -> BadgeAffiliation:
    """``active`` → ``revoked`` (team or the organisation)."""
    require_any(actor, Permission.BADGES_MANAGE, Permission.BADGES_ORG_MANAGE)
    a = await _get(session, public_id)
    await _org_for(session, a, actor)
    if a.status != AffiliationStatus.ACTIVE.value:
        raise _transition_error(a, "revoked")
    await _end(session, actor, a, AffiliationStatus.REVOKED, reason)
    return a


async def set_primary_affiliation(
    session: AsyncSession, actor: Actor, public_id: str
) -> BadgeAffiliation:
    """Make an active affiliation the displayed one (team only)."""
    actor.require(Permission.BADGES_MANAGE)
    a = await _get(session, public_id)
    if a.status != AffiliationStatus.ACTIVE.value:
        raise _transition_error(a, "primary")
    if a.is_primary:
        return a
    await _lock_member(session, a.member_account_id)
    await session.execute(
        update(BadgeAffiliation)
        .where(
            BadgeAffiliation.member_account_id == a.member_account_id,
            BadgeAffiliation.is_primary,
        )
        .values(is_primary=False, updated_at=utcnow())
    )
    await session.flush()
    a.is_primary = True
    a.version += 1
    a.updated_at = utcnow()
    await session.flush()
    await record(
        session,
        account_id=a.member_account_id,
        affiliation_id=a.id,
        action=BadgeAction.AFFILIATION_PRIMARY,
        actor=actor,
    )
    await _publish(session, actor, BadgeAction.AFFILIATION_PRIMARY, a)
    return a


async def _org_for(session: AsyncSession, a: BadgeAffiliation, actor: Actor) -> Badge:
    org = required(await session.get(Badge, a.org_badge_id), "organisation badge")
    _require_manager(actor, org)
    return org


async def _lock_member(session: AsyncSession, member_account_id: int) -> None:
    """Serialise primary changes of one member (partial unique index on is_primary)."""
    await session.execute(
        select(PlatformAccount.id).where(PlatformAccount.id == member_account_id).with_for_update()
    )


_END_ACTIONS = {
    AffiliationStatus.REJECTED: BadgeAction.AFFILIATION_REJECTED,
    AffiliationStatus.REVOKED: BadgeAction.AFFILIATION_REVOKED,
    AffiliationStatus.EXPIRED: BadgeAction.AFFILIATION_EXPIRED,
}


async def _end(
    session: AsyncSession,
    actor: Actor,
    a: BadgeAffiliation,
    status: AffiliationStatus,
    reason: str | None,
    *,
    publish_event: bool = True,
) -> None:
    reason = check_text(reason, field="reason", max_chars=500)
    now = utcnow()
    was_primary = a.is_primary
    old = a.status
    a.status = status.value
    a.is_primary = False
    a.ended_by = actor.label
    a.ended_at = now
    a.end_reason = reason
    a.version += 1
    a.updated_at = now
    await session.flush()
    await record(
        session,
        account_id=a.member_account_id,
        affiliation_id=a.id,
        action=_END_ACTIONS[status],
        actor=actor,
        reason=reason,
        changes={"status": {"from": old, "to": status.value}},
    )
    if publish_event:
        await _publish(session, actor, _END_ACTIONS[status], a)
    if was_primary:
        await _promote_primary(session, a.member_account_id)


async def _promote_primary(session: AsyncSession, member_account_id: int) -> None:
    """Make the oldest remaining active (non-expired) affiliation primary."""
    nxt = await session.scalar(
        select(BadgeAffiliation)
        .where(
            BadgeAffiliation.member_account_id == member_account_id,
            BadgeAffiliation.status == AffiliationStatus.ACTIVE.value,
            BadgeAffiliation.expires_at > utcnow(),
        )
        .order_by(BadgeAffiliation.validated_at, BadgeAffiliation.id)
        .limit(1)
        .with_for_update()
    )
    if nxt is not None and not nxt.is_primary:
        nxt.is_primary = True
        await session.flush()


async def end_org_affiliations(
    session: AsyncSession, actor: Actor, org: Badge, *, reason: str
) -> int:
    """End every live affiliation of an organisation (badge revoked or category changed).

    Internal: called by the badge use cases, which already checked the permission. One
    summary event (``badge.affiliation_changed`` with ``action=org_ended``); one history
    row per affiliation.
    """
    rows = (
        await session.scalars(
            select(BadgeAffiliation)
            .where(
                BadgeAffiliation.org_badge_id == org.id,
                BadgeAffiliation.status.in_([s.value for s in LIVE_AFFILIATION_STATUSES]),
            )
            .with_for_update()
        )
    ).all()
    for a in rows:
        target = (
            AffiliationStatus.REJECTED
            if a.status == AffiliationStatus.PENDING.value
            else AffiliationStatus.REVOKED
        )
        await _end(session, actor, a, target, reason, publish_event=False)
    if rows:
        await publish(
            session,
            "badge.affiliation_changed",
            {
                "action": "org_ended",
                "org_badge": str(org.public_id),
                "count": len(rows),
                "reason": reason,
            },
            aggregate_type="badge",
            aggregate_id=str(org.public_id),
            actor=actor.label,
        )
    return len(rows)


async def end_member_affiliations(
    session: AsyncSession, actor: Actor, member_account_id: int, *, reason: str
) -> int:
    """End every live affiliation of a member (account blocked). Internal."""
    rows = (
        await session.scalars(
            select(BadgeAffiliation)
            .where(
                BadgeAffiliation.member_account_id == member_account_id,
                BadgeAffiliation.status.in_([s.value for s in LIVE_AFFILIATION_STATUSES]),
            )
            .with_for_update()
        )
    ).all()
    for a in rows:
        target = (
            AffiliationStatus.REJECTED
            if a.status == AffiliationStatus.PENDING.value
            else AffiliationStatus.REVOKED
        )
        await _end(session, actor, a, target, reason)
    return len(rows)


async def expire_affiliations(
    session: AsyncSession, actor: Actor, *, now: datetime | None = None, limit: int = 500
) -> int:
    """Active affiliations past ``expires_at`` → ``expired`` (periodic, system)."""
    actor.require(Permission.BADGES_MANAGE)
    now = now or utcnow()
    rows = (
        await session.scalars(
            select(BadgeAffiliation)
            .where(
                BadgeAffiliation.status == AffiliationStatus.ACTIVE.value,
                BadgeAffiliation.expires_at <= now,
            )
            .order_by(BadgeAffiliation.expires_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).all()
    for a in rows:
        await _end(session, actor, a, AffiliationStatus.EXPIRED, "not_reconfirmed")
    return len(rows)


async def list_affiliations(
    session: AsyncSession,
    actor: Actor,
    *,
    status: str | None = None,
    org_ref: AccountRef | None = None,
    member_ref: AccountRef | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Team (``badges:read``): any filter. Organisation: only its own affiliations."""
    require_any(actor, Permission.BADGES_READ, Permission.BADGES_ORG_MANAGE)
    member = aliased(PlatformAccount)
    org_account = aliased(PlatformAccount)
    stmt = (
        select(BadgeAffiliation, member, Badge, org_account)
        .join(member, member.id == BadgeAffiliation.member_account_id)
        .join(Badge, Badge.id == BadgeAffiliation.org_badge_id)
        .join(org_account, org_account.id == Badge.account_id)
        .order_by(BadgeAffiliation.updated_at.desc(), BadgeAffiliation.id.desc())
        .limit(min(max(limit, 1), 500))
        .offset(max(offset, 0))
    )
    if not actor.can(Permission.BADGES_READ):
        org, _ = await org_badge_of_actor(session, actor)
        stmt = stmt.where(BadgeAffiliation.org_badge_id == org.id)
    elif org_ref is not None:
        stmt = stmt.where(
            org_account.platform == org_ref.platform.value,
            org_account.external_id == org_ref.external_id,
        )
    if member_ref is not None:
        stmt = stmt.where(
            member.platform == member_ref.platform.value,
            member.external_id == member_ref.external_id,
        )
    if status is not None:
        stmt = stmt.where(BadgeAffiliation.status == AffiliationStatus(status).value)
    rows: Sequence[Any] = (await session.execute(stmt)).all()
    return [affiliation_payload(a, m, o, oa) for a, m, o, oa in rows]


async def get_affiliation(session: AsyncSession, actor: Actor, public_id: str) -> dict[str, Any]:
    require_any(actor, Permission.BADGES_READ, Permission.BADGES_ORG_MANAGE)
    try:
        pid = uuid.UUID(public_id)
    except ValueError:
        raise NotFound("Affiliation not found", code=ec.BDG_AFFILIATION_NOT_FOUND) from None
    a = await session.scalar(select(BadgeAffiliation).where(BadgeAffiliation.public_id == pid))
    if a is None:
        raise NotFound("Affiliation not found", code=ec.BDG_AFFILIATION_NOT_FOUND)
    member, org, org_account = await _parts(session, a)
    if not actor.can(Permission.BADGES_READ) and actor.id != str(org.public_id):
        raise PermissionDenied(
            "Affiliation belongs to another organisation", code=ec.BDG_NOT_YOUR_AFFILIATION
        )
    return affiliation_payload(a, member, org, org_account)


async def payload_of(session: AsyncSession, a: BadgeAffiliation) -> dict[str, Any]:
    member, org, org_account = await _parts(session, a)
    return affiliation_payload(a, member, org, org_account)


__all__ = [
    "create_affiliation",
    "end_member_affiliations",
    "end_org_affiliations",
    "expire_affiliations",
    "get_affiliation",
    "list_affiliations",
    "payload_of",
    "reconfirm_affiliation",
    "reject_affiliation",
    "revoke_affiliation",
    "set_primary_affiliation",
    "validate_affiliation",
]
