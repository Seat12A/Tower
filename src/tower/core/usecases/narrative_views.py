"""Read models of narratives (gate and bot): list, detail, and the references shown in
group and draft views. Everything is read-only; ids are public UUIDs as strings.

Writes (merge, split, title) live in ``narratives.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain.factcheck.values import NarrativeState
from tower.core.domain.security import Actor, Permission
from tower.core.models.factcheck import ClaimGroup, Verdict
from tower.core.models.factcheck_v2 import (
    Narrative,
    NarrativeEvent,
    NarrativeGroup,
    NarrativeNovelty,
    NarrativeStat,
)
from tower.core.usecases.narratives import get_narrative

__all__ = [
    "OPEN_STATES",
    "draft_narrative_info",
    "group_narrative_by_public",
    "group_narrative_ref",
    "list_narratives",
    "narrative_detail",
]

OPEN_STATES = (
    NarrativeState.ACTIVE.value,
    NarrativeState.WATCH.value,
    NarrativeState.SURGE.value,
    NarrativeState.ALERT.value,
)
JOURNAL_LIMIT = 50
STAT_POINTS = 12
NOVELTY_LIMIT = 10
GROUP_LIMIT = 200
SPANS = ("5m", "1h", "6h")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


async def _public_id_of(session: AsyncSession, model: Any, row_id: int | None) -> str | None:
    if row_id is None:
        return None
    found = await session.scalar(select(model.public_id).where(model.id == row_id))
    return str(found) if found is not None else None


def _stat_point(stat: NarrativeStat) -> dict[str, Any]:
    return {
        "window_end": stat.window_end.isoformat(),
        "posts": stat.posts,
        "flow_posts": stat.flow_posts,
        "share": stat.share,
        "accounts": stat.accounts,
        "platforms": stat.platforms,
        "reach": stat.reach,
        "score": stat.score,
    }


async def _active_group_counts(session: AsyncSession, ids: list[int]) -> dict[int, int]:
    if not ids:
        return {}
    rows = await session.execute(
        select(NarrativeGroup.narrative_id, NarrativeGroup.group_id).where(
            NarrativeGroup.narrative_id.in_(ids), NarrativeGroup.removed_at.is_(None)
        )
    )
    counts: dict[int, int] = {}
    for narrative_id, _ in rows.all():
        counts[narrative_id] = counts.get(narrative_id, 0) + 1
    return counts


async def _latest_hour(session: AsyncSession, ids: list[int]) -> dict[int, NarrativeStat]:
    """Latest ``1h`` window of each narrative."""
    if not ids:
        return {}
    rows = await session.scalars(
        select(NarrativeStat)
        .where(NarrativeStat.narrative_id.in_(ids), NarrativeStat.span == "1h")
        .ext(distinct_on(NarrativeStat.narrative_id))
        .order_by(NarrativeStat.narrative_id, NarrativeStat.window_end.desc())
    )
    return {stat.narrative_id: stat for stat in rows}


def _title(narrative: Narrative) -> dict[str, Any]:
    return {"title": narrative.title, "title_status": narrative.title_status}


async def list_narratives(
    session: AsyncSession,
    actor: Actor,
    *,
    state: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Narratives by last activity. Without ``state``: the open ones (not closed)."""
    actor.require(Permission.DOSSIER_READ)
    stmt = select(Narrative).order_by(Narrative.last_activity_at.desc(), Narrative.id.desc())
    stmt = (
        stmt.where(Narrative.state == state)
        if state
        else stmt.where(Narrative.state.in_(OPEN_STATES))
    )
    narratives = list(await session.scalars(stmt.limit(limit)))
    ids = [n.id for n in narratives]
    counts = await _active_group_counts(session, ids)
    stats = await _latest_hour(session, ids)
    out: list[dict[str, Any]] = []
    for n in narratives:
        stat = stats.get(n.id)
        out.append(
            {
                "id": str(n.public_id),
                **_title(n),
                "state": n.state,
                "theme": n.theme,
                "groups": counts.get(n.id, 0),
                "stats_1h": (
                    {
                        "window_end": stat.window_end.isoformat(),
                        "share": stat.share,
                        "accounts": stat.accounts,
                        "platforms": stat.platforms,
                    }
                    if stat is not None
                    else None
                ),
                "opened_at": _iso(n.opened_at),
                "last_activity_at": _iso(n.last_activity_at),
            }
        )
    return out


async def _groups_view(session: AsyncSession, narrative: Narrative) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(ClaimGroup, Verdict)
            .join(NarrativeGroup, NarrativeGroup.group_id == ClaimGroup.id)
            .outerjoin(Verdict, Verdict.id == ClaimGroup.active_verdict_id)
            .where(NarrativeGroup.narrative_id == narrative.id, NarrativeGroup.removed_at.is_(None))
            .order_by(ClaimGroup.reach.desc(), ClaimGroup.id)
            .limit(GROUP_LIMIT)
        )
    ).all()
    return [
        {
            "id": str(group.public_id),
            "level": group.level,
            "state": group.state,
            "verdict": (
                {
                    "id": str(verdict.public_id),
                    "status": verdict.status,
                    "veracity": verdict.veracity,
                }
                if verdict is not None
                else None
            ),
            "light_signal": group.light_signal,
            "reach": group.reach,
        }
        for group, verdict in rows
    ]


async def _stats_view(
    session: AsyncSession, narrative: Narrative
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for span in SPANS:
        rows = list(
            await session.scalars(
                select(NarrativeStat)
                .where(NarrativeStat.narrative_id == narrative.id, NarrativeStat.span == span)
                .order_by(NarrativeStat.window_end.desc())
                .limit(STAT_POINTS)
            )
        )
        out[span] = [_stat_point(s) for s in reversed(rows)]
    return out


async def _journal_view(session: AsyncSession, narrative: Narrative) -> list[dict[str, Any]]:
    events = list(
        await session.scalars(
            select(NarrativeEvent)
            .where(NarrativeEvent.narrative_id == narrative.id)
            .order_by(NarrativeEvent.id.desc())
            .limit(JOURNAL_LIMIT)
        )
    )
    group_ids = {g for e in events for g in e.group_ids}
    groups: dict[int, str] = {}
    if group_ids:
        found = await session.execute(
            select(ClaimGroup.id, ClaimGroup.public_id).where(ClaimGroup.id.in_(group_ids))
        )
        groups = {i: str(p) for i, p in found.all()}
    others = {e.other_narrative_id for e in events if e.other_narrative_id is not None}
    narratives: dict[int, str] = {}
    if others:
        found = await session.execute(
            select(Narrative.id, Narrative.public_id).where(Narrative.id.in_(others))
        )
        narratives = {i: str(p) for i, p in found.all()}
    return [
        {
            "kind": e.kind,
            "other_narrative_id": narratives.get(e.other_narrative_id or 0),
            "group_ids": [groups[g] for g in e.group_ids if g in groups],
            "from_state": e.from_state,
            "to_state": e.to_state,
            "actor": e.actor,
            "reason": e.reason,
            # Internal integer ids never leave.
            "data": {
                k: v
                for k, v in (e.data or {}).items()
                if not (k.endswith("_id") and isinstance(v, int))
            },
            "created_at": _iso(e.created_at),
        }
        for e in events
    ]


async def _novelty_view(session: AsyncSession, narrative: Narrative) -> list[dict[str, Any]]:
    """Fingerprints that "took" (forced into extraction), most recent first."""
    rows = await session.scalars(
        select(NarrativeNovelty)
        .where(
            NarrativeNovelty.narrative_id == narrative.id, NarrativeNovelty.forced_at.is_not(None)
        )
        .order_by(NarrativeNovelty.forced_at.desc())
        .limit(NOVELTY_LIMIT)
    )
    return [
        {
            "kind": n.kind,
            "fingerprint": n.fingerprint,
            "posts": n.posts,
            "accounts": n.accounts,
            "forced_at": _iso(n.forced_at),
            "first_seen_at": _iso(n.first_seen_at),
        }
        for n in rows
    ]


async def narrative_detail(
    session: AsyncSession, actor: Actor, public_id: uuid.UUID
) -> dict[str, Any]:
    """Narrative with medoids, groups, the stats of the three windows (last 12 points
    each), the journal (50), representative group, previous narrative, split suggestion
    and the novelty fingerprints that took (top 10)."""
    actor.require(Permission.DOSSIER_READ)
    narrative = await get_narrative(session, public_id)
    groups = await _groups_view(session, narrative)
    medoids: list[str] = []
    if narrative.medoid_group_ids:
        found = await session.execute(
            select(ClaimGroup.id, ClaimGroup.public_id).where(
                ClaimGroup.id.in_(narrative.medoid_group_ids)
            )
        )
        by_id = {i: str(p) for i, p in found.all()}
        medoids = [by_id[i] for i in narrative.medoid_group_ids if i in by_id]
    stats = await _stats_view(session, narrative)
    return {
        "id": str(narrative.public_id),
        **_title(narrative),
        "state": narrative.state,
        "theme": narrative.theme,
        "groups_count": len(groups),
        "stats_1h": (
            {k: stats["1h"][-1][k] for k in ("window_end", "share", "accounts", "platforms")}
            if stats["1h"]
            else None
        ),
        "opened_at": _iso(narrative.opened_at),
        "last_activity_at": _iso(narrative.last_activity_at),
        "closed_at": _iso(narrative.closed_at),
        "medoids": medoids,
        "groups": groups,
        "stats": stats,
        "journal": await _journal_view(session, narrative),
        "representative_group": await _public_id_of(
            session, ClaimGroup, narrative.representative_group_id
        ),
        "previous_narrative": await _public_id_of(
            session, Narrative, narrative.previous_narrative_id
        ),
        "merged_into": await _public_id_of(session, Narrative, narrative.merged_into_id),
        "split_suggested": narrative.split_suggested,
        "novelty": await _novelty_view(session, narrative),
    }


async def group_narrative_ref(session: AsyncSession, group_id: int) -> dict[str, Any] | None:
    """``{id, state}`` of the narrative the group belongs to, if any."""
    row = (
        await session.execute(
            select(Narrative.public_id, Narrative.state)
            .join(NarrativeGroup, NarrativeGroup.narrative_id == Narrative.id)
            .where(NarrativeGroup.group_id == group_id, NarrativeGroup.removed_at.is_(None))
            .limit(1)
        )
    ).first()
    return {"id": str(row[0]), "state": row[1]} if row is not None else None


async def group_narrative_by_public(
    session: AsyncSession, group_public_id: uuid.UUID
) -> dict[str, Any] | None:
    """Same as ``group_narrative_ref`` for a group public id."""
    gid = await session.scalar(select(ClaimGroup.id).where(ClaimGroup.public_id == group_public_id))
    return await group_narrative_ref(session, gid) if gid is not None else None


async def draft_narrative_info(session: AsyncSession, group_id: int) -> dict[str, Any] | None:
    """Narrative block of a draft view: ``{id, title, state, groups}``."""
    row = (
        await session.execute(
            select(Narrative)
            .join(NarrativeGroup, NarrativeGroup.narrative_id == Narrative.id)
            .where(NarrativeGroup.group_id == group_id, NarrativeGroup.removed_at.is_(None))
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    counts = await _active_group_counts(session, [row.id])
    return {
        "id": str(row.public_id),
        "title": row.title,
        "state": row.state,
        "groups": counts.get(row.id, 0),
    }
