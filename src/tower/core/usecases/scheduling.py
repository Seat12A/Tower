"""E4/E5 writes of the fact-check scheduler: level changes, group metrics and priorities,
investigation requests, passive transitions, light signals and the pass journal.

Level changes keep the group journal and publish ``claim_group.level_changed`` in the
same transaction (reasons ``promotion``, ``demotion``, ``wave``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain import error_codes as ec
from tower.core.domain.errors import NotFound
from tower.core.domain.factcheck.values import ClaimGroupState, GroupEventKind, LightSignal
from tower.core.domain.security import Actor, Permission
from tower.core.events.outbox import publish
from tower.core.factcheck import names
from tower.core.logging import get_logger
from tower.core.models.factcheck import ClaimGroup, Investigation
from tower.core.models.factcheck_v2 import SchedulerRun
from tower.core.models.ingestion import ContentItem
from tower.core.usecases import claims as claims_uc

__all__ = [
    "GroupMetricsRow",
    "apply_group_metrics",
    "change_level",
    "finish_run",
    "mark_passive",
    "open_run",
    "record_light_signal",
    "schedule_investigation",
    "set_below_threshold",
    "set_priorities",
]

log = get_logger(__name__)

_LEVEL_ORDER = ("N0", "N1", "N2", "N3", "N4")
_LEVEL_REASONS = frozenset({"promotion", "demotion", "wave"})
_MAX_EXPLANATION = 400


@dataclass(frozen=True, slots=True)
class GroupMetricsRow:
    group_id: int
    observers_6h: int
    velocity: float
    views: int


async def open_run(
    session: AsyncSession, actor: Actor, *, now: datetime, rate_per_hour: float, interval_s: int
) -> SchedulerRun:
    """Insert the journal row of a pass (first thing a pass does)."""
    actor.require(Permission.FACTCHECK_REQUEST)
    run = SchedulerRun(started_at=now, rate_per_hour=rate_per_hour, interval_s=interval_s, stats={})
    session.add(run)
    await session.flush()
    return run


async def finish_run(
    session: AsyncSession,
    actor: Actor,
    run: SchedulerRun,
    stats: Mapping[str, Any],
    *,
    now: datetime,
) -> None:
    actor.require(Permission.FACTCHECK_REQUEST)
    run.stats = dict(stats)
    run.finished_at = now
    await session.flush()


async def apply_group_metrics(
    session: AsyncSession, actor: Actor, rows: Sequence[GroupMetricsRow]
) -> int:
    """Store observers, velocity and reach of the groups in ``rows``; groups that still
    carry a non-zero metric but are absent from ``rows`` (no activity left) are zeroed.
    ``updated_at`` is not touched: a metric refresh is not group activity."""
    actor.require(Permission.FACTCHECK_REQUEST)
    ids = [r.group_id for r in rows]
    if rows:
        await session.execute(
            text(
                "UPDATE claim_groups SET observers_6h = :observers, velocity = :velocity,"
                " reach = GREATEST(reach, :views) WHERE id = :id"
            ),
            [
                {
                    "id": r.group_id,
                    "observers": r.observers_6h,
                    "velocity": r.velocity,
                    "views": r.views,
                }
                for r in rows
            ],
        )
    await session.execute(
        text(
            "UPDATE claim_groups SET observers_6h = 0, velocity = 0"
            " WHERE (observers_6h <> 0 OR velocity <> 0) AND id <> ALL(CAST(:ids AS bigint[]))"
        ),
        {"ids": ids},
    )
    return len(ids)


async def set_priorities(session: AsyncSession, actor: Actor, values: Mapping[int, float]) -> None:
    actor.require(Permission.FACTCHECK_REQUEST)
    if not values:
        return
    await session.execute(
        text("UPDATE claim_groups SET priority = :priority WHERE id = :id"),
        [{"id": gid, "priority": value} for gid, value in values.items()],
    )


async def set_below_threshold(
    session: AsyncSession,
    actor: Actor,
    *,
    below: Sequence[int],
    clear: Sequence[int],
    now: datetime,
) -> None:
    """Start the demotion clock of ``below`` (kept when already running) and stop that of
    ``clear``."""
    actor.require(Permission.FACTCHECK_REQUEST)
    if below:
        await session.execute(
            text(
                "UPDATE claim_groups SET below_threshold_since = CAST(:now AS timestamptz)"
                " WHERE id = ANY(CAST(:ids AS bigint[])) AND below_threshold_since IS NULL"
            ),
            {"ids": list(below), "now": now},
        )
    if clear:
        await session.execute(
            text(
                "UPDATE claim_groups SET below_threshold_since = NULL"
                " WHERE id = ANY(CAST(:ids AS bigint[])) AND below_threshold_since IS NOT NULL"
            ),
            {"ids": list(clear)},
        )


async def change_level(
    session: AsyncSession,
    actor: Actor,
    group_id: int,
    level: str,
    *,
    reason: str,
    now: datetime,
    unconfirmed: bool = False,
) -> bool:
    """Set the level of a live group (raise or lower): journal entry and
    ``claim_group.level_changed``. Returns False when nothing changed (already at that
    level, merged group). ``unconfirmed``: the platform could not confirm the metrics."""
    actor.require(Permission.FACTCHECK_REQUEST)
    if level not in _LEVEL_ORDER or reason not in _LEVEL_REASONS:
        raise ValueError(f"Unknown level or level change reason: {level!r}, {reason!r}")
    group = await session.scalar(
        select(ClaimGroup).where(ClaimGroup.id == group_id).with_for_update()
    )
    if group is None:
        raise NotFound("Claim group not found", code=ec.FCK_GROUP_NOT_FOUND, group=group_id)
    if group.state == ClaimGroupState.MERGED.value or group.level == level:
        return False
    previous = group.level
    group.level = level
    group.level_changed_at = now
    group.below_threshold_since = None
    group.updated_at = func.now()
    data: dict[str, Any] = {"unconfirmed": True} if unconfirmed else {}
    await claims_uc.journal_event(
        session,
        actor,
        group,
        GroupEventKind.LEVEL_CHANGED,
        from_level=previous,
        to_level=level,
        reason=reason,
        data=data,
    )
    await publish(
        session,
        names.CLAIM_GROUP_LEVEL_CHANGED,
        {
            "group_id": str(group.public_id),
            "from_level": previous,
            "to_level": level,
            "reason": reason,
            **data,
        },
        aggregate_type="claim_group",
        aggregate_id=str(group.public_id),
        actor=actor.label,
    )
    return True


async def schedule_investigation(
    session: AsyncSession,
    actor: Actor,
    *,
    group_id: int,
    content: ContentItem,
    flags: Mapping[str, Any] | None = None,
) -> Investigation | None:
    """Request the standard investigation of an N2 group the scheduler selected (state
    ``queued``). None when the group is merged or already has an investigation."""
    actor.require(Permission.FACTCHECK_REQUEST)
    group = await session.scalar(
        select(ClaimGroup).where(ClaimGroup.id == group_id).with_for_update()
    )
    if group is None or group.state == ClaimGroupState.MERGED.value:
        return None
    existing = await session.scalar(
        select(Investigation.id).where(Investigation.claim_group_id == group.id).limit(1)
    )
    if existing is not None:
        return None
    group.state = ClaimGroupState.QUEUED.value
    group.updated_at = func.now()
    return await claims_uc.request_investigation(
        session,
        actor,
        group=group,
        content=content,
        level=group.level,
        requested_by=actor.id or names.FACTCHECK_ACTOR,
        flags=flags,
    )


async def mark_passive(session: AsyncSession, actor: Actor, *, cutoff: datetime) -> int:
    """Verified groups whose active verdict was validated before ``cutoff`` become
    ``passive`` (no event: only the scheduler's attention changes)."""
    actor.require(Permission.FACTCHECK_REQUEST)
    result = await session.execute(
        text(
            "UPDATE claim_groups g SET state = 'passive', updated_at = now()"
            " FROM verdicts v WHERE v.id = g.active_verdict_id AND g.state = 'verified'"
            " AND v.validated_at < CAST(:cutoff AS timestamptz) RETURNING g.id"
        ),
        {"cutoff": cutoff},
    )
    return len(result.all())


async def record_light_signal(
    session: AsyncSession,
    actor: Actor,
    group_id: int,
    signal: LightSignal,
    *,
    explanation: str,
    evidence: Sequence[Mapping[str, str]],
    now: datetime,
    profile_version: int = 0,
    dossier_investigation_id: int | None = None,
) -> bool:
    """Store the E5 signal of a group (never published). False for a merged group."""
    actor.require(Permission.FACTCHECK_REQUEST)
    group = await session.scalar(
        select(ClaimGroup).where(ClaimGroup.id == group_id).with_for_update()
    )
    if group is None:
        raise NotFound("Claim group not found", code=ec.FCK_GROUP_NOT_FOUND, group=group_id)
    if group.state == ClaimGroupState.MERGED.value:
        return False
    group.light_signal = signal.value
    group.light_checked_at = now
    group.light_evidence = {
        "explanation": explanation[:_MAX_EXPLANATION],
        "evidence": [dict(item) for item in evidence],
        "profile_version": profile_version,
        "dossier_investigation_id": dossier_investigation_id,
    }
    return True
