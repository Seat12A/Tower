"""Manual level overrides of a content, a group or an account (docs/factcheck/niveaux.md).

An override forces a level for a limited time. Raising to N4 opens the N4 investigation;
a lowering never lowers a group whose investigation is running (the level is monotone while
it runs). Retrogradation is never automatic here: revoking only ends the override.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain import error_codes as ec
from tower.core.domain.errors import InvalidInput, NotFound
from tower.core.domain.factcheck.profiles import LEVELS
from tower.core.domain.factcheck.values import GroupEventKind, InvestigationStage, OverrideTarget
from tower.core.domain.platforms import Platform
from tower.core.domain.security import Actor, Permission
from tower.core.events.outbox import publish
from tower.core.factcheck import names
from tower.core.logging import get_logger
from tower.core.models.factcheck import (
    ClaimGroup,
    ClaimGroupMember,
    ContentClaim,
    Investigation,
)
from tower.core.models.factcheck_v1 import ClaimGroupEvent, LevelOverride
from tower.core.models.ingestion import ContentItem, PlatformAccount
from tower.core.usecases.claims import promote_to_n4

__all__ = [
    "OverrideResult",
    "create_override",
    "list_overrides",
    "revoke_override",
    "target_labels",
]

log = get_logger(__name__)

MAX_HOURS = 720.0
_FINISHED = (
    InvestigationStage.COMPLETED.value,
    InvestigationStage.ASSEMBLED.value,
    InvestigationStage.FAILED.value,
)
_RANK = {level: i for i, level in enumerate(LEVELS)}


@dataclass(frozen=True, slots=True)
class OverrideResult:
    override: LevelOverride
    target: str
    """Public id (content, group) or external id / handle (account) of the target."""
    applied: bool
    """The level of every targeted group now matches the override. False for an account
    (recorded only), a content without group yet, or a lowering blocked by a running
    investigation."""
    groups_changed: int


def _invalid(message: str, **details: object) -> InvalidInput:
    return InvalidInput(message, code=ec.FCK_OVERRIDE_INVALID, **details)


def _not_found(target_type: str, ref: str) -> NotFound:
    return NotFound(
        "Override target not found",
        code=ec.FCK_OVERRIDE_NOT_FOUND,
        target_type=target_type,
        target=ref[:80],
    )


def _split_ref(ref: str) -> tuple[str, str]:
    platform, sep, rest = ref.partition(":")
    if not sep or not rest or platform not in {p.value for p in Platform}:
        raise _invalid("Target must look like '<platform>:<id>'", target=ref[:80])
    return platform, rest


async def _resolve(session: AsyncSession, target_type: OverrideTarget, ref: str) -> tuple[int, str]:
    """``(internal id, public label)`` of the target, or ``NotFound``."""
    ref = ref.strip()
    if not ref:
        raise _invalid("A target is required")
    if target_type is OverrideTarget.GROUP:
        try:
            public = uuid.UUID(ref)
        except ValueError:
            raise _invalid("A group target is a public id", target=ref[:80]) from None
        gid = await session.scalar(select(ClaimGroup.id).where(ClaimGroup.public_id == public))
        if gid is None:
            raise _not_found("group", ref)
        return gid, str(public)
    if target_type is OverrideTarget.CONTENT:
        try:
            public = uuid.UUID(ref)
        except ValueError:
            platform, external = _split_ref(ref)
            cid = await session.scalar(
                select(ContentItem.id).where(
                    ContentItem.platform == platform, ContentItem.external_id == external
                )
            )
            label = external
        else:
            cid = await session.scalar(
                select(ContentItem.id).where(ContentItem.public_id == public)
            )
            label = str(public)
        if cid is None:
            raise _not_found("content", ref)
        return cid, label
    platform, rest = _split_ref(ref)
    stmt = select(PlatformAccount.id, PlatformAccount.external_id, PlatformAccount.handle).where(
        PlatformAccount.platform == platform
    )
    if rest.startswith("@"):
        handle = rest[1:].strip()
        if not handle:
            raise _invalid("Empty handle", target=ref[:80])
        stmt = stmt.where(func.lower(PlatformAccount.handle) == handle.lower())
    else:
        stmt = stmt.where(PlatformAccount.external_id == rest)
    account = (await session.execute(stmt.limit(1))).first()
    if account is None:
        raise _not_found("account", ref)
    return account[0], str(account[1] or f"@{account[2]}")


def _blocked(running: bool, current: str, wanted: str) -> bool:
    return running and _RANK[wanted] < _RANK[current]


async def _set_group_level(
    session: AsyncSession,
    actor: Actor,
    *,
    group: ClaimGroup,
    level: str,
    reason: str,
    content: ContentItem | None,
) -> bool | None:
    """Apply ``level`` to ``group``. True: changed, False: already at that level,
    None: lowering refused (investigation running)."""
    current = group.level
    if current == level:
        return False
    running = (
        await session.scalar(
            select(Investigation.id)
            .where(
                Investigation.claim_group_id == group.id,
                Investigation.stage.not_in(_FINISHED),
            )
            .limit(1)
        )
        is not None
    )
    if _blocked(running, current, level):
        log.info("level_override.lowering_deferred", group=group.id, level=current)
        return None
    if level == "N4":
        if content is None:
            content = await _latest_content(session, group)
        if content is not None:
            await promote_to_n4(
                session, actor, group=group, content=content, requested_by=actor.label
            )
        else:
            group.level = "N4"
            group.updated_at = func.now()
    else:
        group.level = level
        group.updated_at = func.now()
    session.add(
        ClaimGroupEvent(
            group_id=group.id,
            kind=GroupEventKind.LEVEL_CHANGED.value,
            from_level=current,
            to_level=level,
            actor=actor.label,
            reason=reason,
            data={"source": "override"},
        )
    )
    await session.flush()
    await publish(
        session,
        names.CLAIM_GROUP_LEVEL_CHANGED,
        {
            "group_id": str(group.public_id),
            "from_level": current,
            "to_level": level,
            "reason": "override",
        },
        aggregate_type="claim_group",
        aggregate_id=str(group.public_id),
        actor=actor.label,
    )
    return True


async def _latest_content(session: AsyncSession, group: ClaimGroup) -> ContentItem | None:
    """Most recent content linked to the group's representative claim."""
    return await session.scalar(
        select(ContentItem)
        .join(ContentClaim, ContentClaim.content_id == ContentItem.id)
        .where(ContentClaim.claim_id == group.representative_claim_id)
        .order_by(ContentClaim.created_at.desc(), ContentItem.id.desc())
        .limit(1)
    )


async def create_override(
    session: AsyncSession,
    actor: Actor,
    *,
    target_type: OverrideTarget,
    target_ref: str,
    level: str,
    hours: float,
    reason: str,
    now: datetime | None = None,
) -> OverrideResult:
    """Record an override and apply it now to the groups it targets."""
    actor.require(Permission.LEVELS_OVERRIDE)
    reason = (reason or "").strip()
    if not reason:
        raise _invalid("A reason is required")
    if level not in LEVELS:
        raise _invalid("Unknown level", level=str(level)[:16])
    if not 0 < hours <= MAX_HOURS:
        raise _invalid("Duration out of range (0 < hours <= 720)", hours=hours)
    now = now or datetime.now(UTC)
    target_id, label = await _resolve(session, target_type, target_ref)
    override = LevelOverride(
        public_id=uuid.uuid4(),
        target_type=target_type.value,
        target_id=target_id,
        level=level,
        expires_at=now + timedelta(hours=hours),
        author=actor.label,
        reason=reason,
    )
    session.add(override)
    await session.flush()

    changed = 0
    blocked = False
    groups: list[ClaimGroup] = []
    content: ContentItem | None = None
    if target_type is OverrideTarget.GROUP:
        group = await session.get(ClaimGroup, target_id, with_for_update=True)
        groups = [group] if group is not None else []
    elif target_type is OverrideTarget.CONTENT:
        content = await session.get(ContentItem, target_id)
        member_groups = (
            select(ClaimGroupMember.group_id)
            .join(ContentClaim, ContentClaim.claim_id == ClaimGroupMember.claim_id)
            .where(ContentClaim.content_id == target_id)
        )
        groups = list(
            await session.scalars(
                select(ClaimGroup)
                .where(ClaimGroup.id.in_(member_groups), ClaimGroup.state != "merged")
                .order_by(ClaimGroup.id)
                .with_for_update()
            )
        )
    for group in groups:
        outcome = await _set_group_level(
            session, actor, group=group, level=level, reason=reason, content=content
        )
        if outcome is None:
            blocked = True
        elif outcome:
            changed += 1
    applied = bool(groups) and not blocked

    await publish(
        session,
        names.LEVEL_OVERRIDE_CREATED,
        {
            "override_id": str(override.public_id),
            "target_type": target_type.value,
            "target_id": label,
            "level": level,
            "expires_at": override.expires_at.isoformat(),
            "actor": actor.label,
            "reason": reason,
        },
        aggregate_type="level_override",
        aggregate_id=str(override.public_id),
        actor=actor.label,
    )
    log.info("level_override.created", target_type=target_type.value, level=level, applied=applied)
    return OverrideResult(override, label, applied, changed)


async def revoke_override(
    session: AsyncSession, actor: Actor, *, override_id: uuid.UUID, reason: str
) -> LevelOverride:
    """End an override early. The group levels are left as they are."""
    actor.require(Permission.LEVELS_OVERRIDE)
    reason = (reason or "").strip()
    if not reason:
        raise _invalid("A reason is required")
    override = await session.scalar(
        select(LevelOverride).where(LevelOverride.public_id == override_id).with_for_update()
    )
    if override is None:
        raise NotFound(
            "Level override not found", code=ec.FCK_OVERRIDE_NOT_FOUND, override=str(override_id)
        )
    if override.revoked_at is not None:
        raise _invalid("Override already revoked", override=str(override_id))
    override.revoked_at = datetime.now(UTC)
    override.revoked_by = actor.label
    await session.flush()
    log.info("level_override.revoked", override=str(override_id), reason_length=len(reason))
    return override


async def list_overrides(
    session: AsyncSession, actor: Actor, *, active: bool = True, limit: int = 100
) -> list[LevelOverride]:
    """Overrides, newest first; ``active``: not revoked and not expired."""
    actor.require(Permission.DOSSIER_READ)
    stmt = select(LevelOverride).order_by(LevelOverride.id.desc()).limit(limit)
    if active:
        stmt = stmt.where(
            LevelOverride.revoked_at.is_(None), LevelOverride.expires_at > datetime.now(UTC)
        )
    return list(await session.scalars(stmt))


async def target_labels(
    session: AsyncSession, overrides: Sequence[LevelOverride]
) -> dict[int, str]:
    """Public label of each override target (override id -> public id, external id or
    handle), in one query per target kind; a vanished target gets an empty label."""
    labels: dict[int, str] = {}
    for kind in OverrideTarget:
        mine = [o for o in overrides if o.target_type == kind.value]
        if not mine:
            continue
        ids = {o.target_id for o in mine}
        found: dict[int, str] = {}
        if kind is OverrideTarget.GROUP:
            rows = await session.execute(
                select(ClaimGroup.id, ClaimGroup.public_id).where(ClaimGroup.id.in_(ids))
            )
            found = {i: str(p) for i, p in rows}
        elif kind is OverrideTarget.CONTENT:
            rows = await session.execute(
                select(ContentItem.id, ContentItem.public_id).where(ContentItem.id.in_(ids))
            )
            found = {i: str(p) for i, p in rows}
        else:
            accounts = await session.execute(
                select(
                    PlatformAccount.id, PlatformAccount.external_id, PlatformAccount.handle
                ).where(PlatformAccount.id.in_(ids))
            )
            found = {a.id: str(a.external_id or f"@{a.handle}") for a in accounts}
        for o in mine:
            labels[o.id] = found.get(o.target_id, "")
    return labels
