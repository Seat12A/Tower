"""Narratives: creation, attachment, state changes, medoids, titles, merge and split.

A narrative groups claim groups that tell the same story. It shares an evidence dossier
and an alert, never a verdict (docs/factcheck/vagues.md). Every change is journaled in
``narrative_events``; merges, splits and surge transitions publish an event in the same
transaction. Automatic operations use a system actor; merge and split need
``GROUPS_MANAGE``, title validation ``DOSSIER_REVIEW``.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain import error_codes as ec
from tower.core.domain.errors import InvalidInput, NotFound
from tower.core.domain.factcheck.values import (
    ClaimGroupState,
    NarrativeEventKind,
    NarrativeState,
    TitleStatus,
)
from tower.core.domain.factcheck.waves import WAVE_STATES, elect_medoids
from tower.core.domain.security import Actor, Permission
from tower.core.events.outbox import publish
from tower.core.factcheck import names
from tower.core.logging import get_logger
from tower.core.models.factcheck import Claim, ClaimGroup
from tower.core.models.factcheck_v2 import Narrative, NarrativeEvent, NarrativeGroup

__all__ = [
    "NARRATIVE_CONTENTS",
    "attach_group",
    "change_state",
    "close_narrative",
    "create_narrative",
    "detach_group",
    "elect_narrative_medoids",
    "get_narrative",
    "group_in_surge",
    "merge_narratives",
    "set_medoids",
    "set_title",
    "split_narrative",
    "surge_drafts_today",
    "validate_title",
]

log = get_logger(__name__)

OPEN_STATES = (
    NarrativeState.ACTIVE.value,
    NarrativeState.WATCH.value,
    NarrativeState.SURGE.value,
    NarrativeState.ALERT.value,
)
MEDOID_ELECTION_INTERVAL = timedelta(days=1)

NARRATIVE_CONTENTS = """
SELECT DISTINCT ng.narrative_id, cc.content_id
FROM narrative_groups ng
JOIN claim_group_members m ON m.group_id = ng.group_id AND m.removed_at IS NULL
JOIN content_claims cc ON cc.claim_id = m.claim_id
WHERE ng.removed_at IS NULL AND ng.narrative_id = ANY(:nids)
"""
"""SQL: ``(narrative_id, content_id)`` of the contents carrying a claim of an active group
of the given narratives (bind ``nids``)."""


# ------------------------------------------------------------------------- lookup


async def get_narrative(
    session: AsyncSession, public_id: uuid.UUID, *, lock: bool = False
) -> Narrative:
    query = select(Narrative).where(Narrative.public_id == public_id)
    if lock:
        query = query.with_for_update()
    narrative = await session.scalar(query)
    if narrative is None:
        raise NotFound(
            "Narrative not found", code=ec.FCK_NARRATIVE_NOT_FOUND, narrative=str(public_id)
        )
    return narrative


async def _journal(
    session: AsyncSession,
    actor: Actor,
    narrative: Narrative,
    kind: NarrativeEventKind,
    *,
    other: Narrative | None = None,
    group_ids: Sequence[int] = (),
    from_state: str | None = None,
    to_state: str | None = None,
    reason: str | None = None,
    data: Mapping[str, Any] | None = None,
) -> None:
    session.add(
        NarrativeEvent(
            narrative_id=narrative.id,
            kind=kind.value,
            other_narrative_id=other.id if other is not None else None,
            group_ids=list(group_ids),
            from_state=from_state,
            to_state=to_state,
            actor=actor.label,
            reason=reason,
            data=dict(data or {}),
        )
    )
    await session.flush()


async def _group_public_ids(session: AsyncSession, group_ids: Sequence[int]) -> list[str]:
    if not group_ids:
        return []
    rows = await session.execute(
        select(ClaimGroup.id, ClaimGroup.public_id).where(ClaimGroup.id.in_(list(group_ids)))
    )
    by_id = {int(i): str(p) for i, p in rows.all()}
    return [by_id[i] for i in group_ids if i in by_id]


async def active_group_ids(session: AsyncSession, narrative_id: int) -> list[int]:
    rows = await session.scalars(
        select(NarrativeGroup.group_id)
        .where(NarrativeGroup.narrative_id == narrative_id, NarrativeGroup.removed_at.is_(None))
        .order_by(NarrativeGroup.group_id)
    )
    return [int(r) for r in rows.all()]


# ------------------------------------------------------------------ create, attach


async def create_narrative(
    session: AsyncSession,
    actor: Actor,
    *,
    group: ClaimGroup,
    previous: Narrative | None = None,
    signals: Mapping[str, Any] | None = None,
) -> Narrative:
    """New active narrative founded by ``group`` (founder = medoids = [group])."""
    now = datetime.now(UTC)
    theme = await session.scalar(
        select(Claim.theme).where(Claim.id == group.representative_claim_id)
    )
    narrative = Narrative(
        public_id=uuid.uuid4(),
        state=NarrativeState.ACTIVE.value,
        title_status=TitleStatus.NONE.value,
        theme=theme,
        founder_group_id=group.id,
        medoid_group_ids=[group.id],
        medoids_elected_at=now,
        previous_narrative_id=previous.id if previous is not None else None,
        state_changed_at=now,
        opened_at=now,
        last_activity_at=now,
    )
    session.add(narrative)
    await session.flush()
    session.add(
        NarrativeGroup(
            narrative_id=narrative.id,
            group_id=group.id,
            signals=dict(signals or {}),
            reason="founder",
        )
    )
    await session.flush()
    await _journal(
        session,
        actor,
        narrative,
        NarrativeEventKind.CREATED,
        other=previous,
        group_ids=[group.id],
        to_state=narrative.state,
        reason="resurgence" if previous is not None else "new_story",
        data={"previous_narrative_id": str(previous.public_id)} if previous is not None else None,
    )
    log.info("narrative.created", narrative=narrative.id, previous=bool(previous))
    return narrative


async def attach_group(
    session: AsyncSession,
    actor: Actor,
    narrative: Narrative,
    group: ClaimGroup,
    *,
    signals: Mapping[str, Any],
    reason: str = "match",
) -> None:
    """Attach ``group`` (it must not belong to another narrative: detach it first)."""
    if narrative.state == NarrativeState.CLOSED.value or narrative.merged_into_id is not None:
        raise InvalidInput(
            "Cannot attach a group to a closed or merged narrative",
            code=ec.FCK_NARRATIVE_INVALID,
            narrative=str(narrative.public_id),
        )
    session.add(
        NarrativeGroup(
            narrative_id=narrative.id, group_id=group.id, signals=dict(signals), reason=reason
        )
    )
    narrative.last_activity_at = datetime.now(UTC)
    narrative.updated_at = datetime.now(UTC)
    await session.flush()
    await _journal(
        session,
        actor,
        narrative,
        NarrativeEventKind.ATTACHED,
        group_ids=[group.id],
        reason=reason,
        data=signals,
    )


async def detach_group(
    session: AsyncSession,
    actor: Actor,
    narrative: Narrative,
    group: ClaimGroup,
    *,
    reason: str,
) -> bool:
    """Close the membership of ``group`` (the row is kept with ``removed_at``)."""
    if group.id == narrative.founder_group_id:
        raise InvalidInput(
            "The founder group cannot leave its narrative",
            code=ec.FCK_NARRATIVE_INVALID,
            narrative=str(narrative.public_id),
        )
    result = await session.execute(
        update(NarrativeGroup)
        .where(
            NarrativeGroup.narrative_id == narrative.id,
            NarrativeGroup.group_id == group.id,
            NarrativeGroup.removed_at.is_(None),
        )
        .values(removed_at=func.now())
    )
    if result.rowcount == 0:  # type: ignore[attr-defined]
        return False
    await _journal(
        session, actor, narrative, NarrativeEventKind.DETACHED, group_ids=[group.id], reason=reason
    )
    await _refresh_medoids_after_removal(session, actor, narrative, [group.id])
    return True


# ------------------------------------------------------------------------ medoids


async def set_medoids(
    session: AsyncSession,
    actor: Actor,
    narrative: Narrative,
    medoid_ids: Sequence[int],
    *,
    reason: str,
) -> bool:
    """Store the medoids (founder first, at most 3). Journals only a real change."""
    now = datetime.now(UTC)
    new = list(medoid_ids)[:3]
    narrative.medoids_elected_at = now
    if new == list(narrative.medoid_group_ids):
        return False
    previous = list(narrative.medoid_group_ids)
    narrative.medoid_group_ids = new
    narrative.updated_at = now
    await session.flush()
    await _journal(
        session,
        actor,
        narrative,
        NarrativeEventKind.MEDOIDS_CHANGED,
        group_ids=new,
        reason=reason,
        data={"previous": previous},
    )
    return True


async def elect_narrative_medoids(
    session: AsyncSession,
    actor: Actor,
    narrative: Narrative,
    *,
    force: bool = False,
    now: datetime | None = None,
) -> bool:
    """Founder (fixed) + the 2 groups of highest reach; at most once a day unless
    ``force``. Returns True when the medoids changed."""
    now = now or datetime.now(UTC)
    if (
        not force
        and narrative.medoids_elected_at is not None
        and now - narrative.medoids_elected_at < MEDOID_ELECTION_INTERVAL
    ):
        return False
    rows = await session.execute(
        select(ClaimGroup.id, ClaimGroup.reach)
        .join(NarrativeGroup, NarrativeGroup.group_id == ClaimGroup.id)
        .where(
            NarrativeGroup.narrative_id == narrative.id,
            NarrativeGroup.removed_at.is_(None),
            ClaimGroup.state != ClaimGroupState.MERGED.value,
        )
    )
    medoids = elect_medoids(narrative.founder_group_id, [(int(g), int(r)) for g, r in rows.all()])
    return await set_medoids(session, actor, narrative, medoids, reason="election")


async def _refresh_medoids_after_removal(
    session: AsyncSession, actor: Actor, narrative: Narrative, removed: Sequence[int]
) -> None:
    if set(removed) & set(narrative.medoid_group_ids):
        await elect_narrative_medoids(session, actor, narrative, force=True)


# ------------------------------------------------------------------------ state


async def _surge_report(
    session: AsyncSession, narrative: Narrative, now: datetime
) -> dict[str, Any]:
    since = narrative.surge_started_at or narrative.opened_at
    row = (
        await session.execute(
            text(
                f"WITH nc AS ({NARRATIVE_CONTENTS}) "  # noqa: S608 (constant fragment)
                "SELECT count(*), COALESCE(array_agg(DISTINCT o.platform), '{}') "
                "FROM nc JOIN observations o ON o.content_id = nc.content_id "
                "AND o.observed_at >= :since"
            ),
            {"nids": [narrative.id], "since": since},
        )
    ).one()
    cost = await session.scalar(
        text(
            "SELECT COALESCE(sum(i.cost_usd_micros), 0) FROM investigations i "
            "JOIN narrative_groups ng ON ng.group_id = i.claim_group_id "
            "AND ng.narrative_id = :n AND ng.removed_at IS NULL"
        ),
        {"n": narrative.id},
    )
    first_draft = await session.scalar(
        text(
            "SELECT min(v.drafted_at) FROM verdicts v "
            "JOIN narrative_groups ng ON ng.group_id = v.claim_group_id "
            "AND ng.narrative_id = :n AND ng.removed_at IS NULL "
            "WHERE v.drafted_at >= :since"
        ),
        {"n": narrative.id, "since": since},
    )
    return {
        "narrative_id": str(narrative.public_id),
        "duration_s": max(0, int((now - since).total_seconds())),
        "posts": int(row[0]),
        "platforms": sorted(row[1]),
        "cost_usd_micros": int(cost or 0),
        "first_draft_delay_s": (
            max(0, int((first_draft - since).total_seconds())) if first_draft is not None else None
        ),
    }


async def _publish_surge_ended(
    session: AsyncSession, actor: Actor, narrative: Narrative, now: datetime
) -> dict[str, Any]:
    report = await _surge_report(session, narrative, now)
    await publish(
        session,
        names.NARRATIVE_SURGE_ENDED,
        report,
        aggregate_type="narrative",
        aggregate_id=str(narrative.public_id),
        actor=actor.label,
    )
    return report


async def change_state(
    session: AsyncSession,
    actor: Actor,
    narrative: Narrative,
    new_state: str,
    *,
    reason: str,
    stats: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> bool:
    """Move ``narrative`` to ``new_state`` (never to ``closed``: use ``close_narrative``).

    Entering ``surge``/``alert`` sets ``surge_started_at`` and publishes
    ``narrative.surge_detected`` (``stats``: ``share``, ``accounts``, ``platforms``);
    leaving a wave state for ``active`` publishes ``narrative.surge_ended`` with the
    report. Returns False when nothing changed."""
    if new_state not in OPEN_STATES:
        raise InvalidInput(
            f"Invalid narrative state {new_state!r}",
            code=ec.FCK_NARRATIVE_INVALID,
            narrative=str(narrative.public_id),
        )
    old = narrative.state
    if old == new_state:
        return False
    if old == NarrativeState.CLOSED.value or narrative.merged_into_id is not None:
        raise InvalidInput(
            "A closed or merged narrative cannot change state",
            code=ec.FCK_NARRATIVE_INVALID,
            narrative=str(narrative.public_id),
        )
    now = now or datetime.now(UTC)
    report: dict[str, Any] | None = None
    if old in WAVE_STATES and new_state not in WAVE_STATES:
        report = await _publish_surge_ended(session, actor, narrative, now)
    narrative.state = new_state
    narrative.state_changed_at = now
    narrative.updated_at = now
    narrative.below_watch_since = None
    if new_state in WAVE_STATES and old not in WAVE_STATES:
        narrative.surge_started_at = now
    await session.flush()
    data: dict[str, Any] = dict(stats or {})
    if report is not None:
        data["report"] = report
    await _journal(
        session,
        actor,
        narrative,
        NarrativeEventKind.STATE_CHANGED,
        from_state=old,
        to_state=new_state,
        reason=reason,
        data=data,
    )
    if new_state in WAVE_STATES:
        representative = (
            await _group_public_ids(session, [narrative.representative_group_id])
            if narrative.representative_group_id is not None
            else []
        )
        await publish(
            session,
            names.NARRATIVE_SURGE_DETECTED,
            {
                "narrative_id": str(narrative.public_id),
                "tier": new_state,
                "share": float((stats or {}).get("share", 0.0)),
                "accounts": int((stats or {}).get("accounts", 0)),
                "platforms": list((stats or {}).get("platforms", [])),
                "title": narrative.title,
                "representative_group_id": representative[0] if representative else None,
            },
            aggregate_type="narrative",
            aggregate_id=str(narrative.public_id),
            actor=actor.label,
        )
    log.info("narrative.state_changed", narrative=narrative.id, from_state=old, to_state=new_state)
    return True


async def set_below_watch(
    session: AsyncSession, narrative: Narrative, since: datetime | None
) -> None:
    """Remember when a wave narrative fell below the watch tier (None: it did not)."""
    if narrative.below_watch_since != since:
        narrative.below_watch_since = since
        await session.flush()


async def close_narrative(
    session: AsyncSession,
    actor: Actor,
    narrative: Narrative,
    *,
    reason: str,
    now: datetime | None = None,
) -> bool:
    """Close a narrative (inactivity). A later match creates a new one that points here."""
    if narrative.state == NarrativeState.CLOSED.value:
        return False
    now = now or datetime.now(UTC)
    old = narrative.state
    report: dict[str, Any] | None = None
    if old in WAVE_STATES:
        report = await _publish_surge_ended(session, actor, narrative, now)
    narrative.state = NarrativeState.CLOSED.value
    narrative.closed_at = now
    narrative.state_changed_at = now
    narrative.updated_at = now
    narrative.split_suggested = False
    await session.flush()
    await _journal(
        session,
        actor,
        narrative,
        NarrativeEventKind.CLOSED,
        from_state=old,
        to_state=narrative.state,
        reason=reason,
        data={"report": report} if report is not None else None,
    )
    log.info("narrative.closed", narrative=narrative.id, reason=reason)
    return True


# ------------------------------------------------------------------------- title


async def set_title(
    session: AsyncSession,
    actor: Actor,
    narrative: Narrative,
    *,
    title_fr: str,
    title_en: str,
) -> bool:
    """Draft title (French) written by the model; the English one stays in the journal.
    A validated title is never overwritten."""
    if narrative.title_status == TitleStatus.VALIDATED.value:
        return False
    narrative.title = title_fr
    narrative.title_status = TitleStatus.DRAFT.value
    narrative.updated_at = datetime.now(UTC)
    await session.flush()
    await _journal(
        session,
        actor,
        narrative,
        NarrativeEventKind.TITLE_SET,
        reason="model",
        data={"title_fr": title_fr, "title_en": title_en, "status": TitleStatus.DRAFT.value},
    )
    return True


async def validate_title(
    session: AsyncSession,
    actor: Actor,
    narrative_id: uuid.UUID,
    *,
    title: str | None = None,
) -> Narrative:
    """A reviewer validates the draft title, or replaces it by ``title``."""
    actor.require(Permission.DOSSIER_REVIEW)
    narrative = await get_narrative(session, narrative_id, lock=True)
    new_title = (title or "").strip() or narrative.title
    if not new_title:
        raise InvalidInput(
            "The narrative has no title to validate",
            code=ec.FCK_NARRATIVE_INVALID,
            narrative=str(narrative_id),
        )
    narrative.title = new_title
    narrative.title_status = TitleStatus.VALIDATED.value
    narrative.updated_at = datetime.now(UTC)
    await session.flush()
    await _journal(
        session,
        actor,
        narrative,
        NarrativeEventKind.TITLE_SET,
        reason="validated" if not title else "edited",
        data={"title_fr": new_title, "status": TitleStatus.VALIDATED.value},
    )
    return narrative


# ------------------------------------------------------------------ merge and split


async def _lock_pair(session: AsyncSession, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, Narrative]:
    rows = await session.scalars(
        select(Narrative)
        .where(Narrative.public_id.in_(list(ids)))
        .order_by(Narrative.id)
        .with_for_update()
    )
    found = {n.public_id: n for n in rows.all()}
    for public_id in ids:
        if public_id not in found:
            raise NotFound(
                "Narrative not found", code=ec.FCK_NARRATIVE_NOT_FOUND, narrative=str(public_id)
            )
    return found


def _require_open(narrative: Narrative) -> None:
    if narrative.state == NarrativeState.CLOSED.value or narrative.merged_into_id is not None:
        raise InvalidInput(
            "The narrative is closed or already merged",
            code=ec.FCK_NARRATIVE_INVALID,
            narrative=str(narrative.public_id),
        )


async def merge_narratives(
    session: AsyncSession,
    actor: Actor,
    *,
    target_id: uuid.UUID,
    source_id: uuid.UUID,
    reason: str,
) -> Narrative:
    """Absorb ``source`` into ``target``: groups move (history kept with ``removed_at``),
    the source is closed with ``merged_into_id``. Undo by splitting the moved groups."""
    actor.require(Permission.GROUPS_MANAGE)
    if target_id == source_id:
        raise InvalidInput(
            "A narrative cannot be merged into itself",
            code=ec.FCK_NARRATIVE_INVALID,
            narrative=str(target_id),
        )
    found = await _lock_pair(session, [target_id, source_id])
    target, source = found[target_id], found[source_id]
    _require_open(target)
    _require_open(source)
    now = datetime.now(UTC)
    moved = await active_group_ids(session, source.id)
    if source.state in WAVE_STATES:
        await _publish_surge_ended(session, actor, source, now)
    await session.execute(
        update(NarrativeGroup)
        .where(NarrativeGroup.narrative_id == source.id, NarrativeGroup.removed_at.is_(None))
        .values(removed_at=func.now())
    )
    await session.flush()
    for group_id in moved:
        session.add(
            NarrativeGroup(
                narrative_id=target.id,
                group_id=group_id,
                signals={"merged_from": str(source.public_id)},
                reason="merge",
            )
        )
    source_state = source.state
    source.state = NarrativeState.CLOSED.value
    source.closed_at = now
    source.state_changed_at = now
    source.merged_into_id = target.id
    source.split_suggested = False
    source.updated_at = now
    target.last_activity_at = max(target.last_activity_at, source.last_activity_at)
    target.updated_at = now
    await session.flush()
    await elect_narrative_medoids(session, actor, target, force=True)
    for narrative, other in ((target, source), (source, target)):
        await _journal(
            session,
            actor,
            narrative,
            NarrativeEventKind.MERGED,
            other=other,
            group_ids=moved,
            from_state=source_state if narrative is source else None,
            to_state=narrative.state if narrative is source else None,
            reason=reason,
        )
    await publish(
        session,
        names.NARRATIVE_MERGED,
        {
            "target_id": str(target.public_id),
            "source_id": str(source.public_id),
            "group_ids": await _group_public_ids(session, moved),
            "actor": actor.label,
            "reason": reason,
        },
        aggregate_type="narrative",
        aggregate_id=str(target.public_id),
        actor=actor.label,
    )
    log.info("narrative.merged", target=target.id, source=source.id, groups=len(moved))
    return target


async def split_narrative(
    session: AsyncSession,
    actor: Actor,
    *,
    narrative_id: uuid.UUID,
    group_ids: Sequence[uuid.UUID],
    reason: str,
) -> Narrative:
    """Move ``group_ids`` out of the narrative into a new one (founded by the group of
    highest reach). The founder cannot leave and at least one group must stay. Returns
    the new narrative."""
    actor.require(Permission.GROUPS_MANAGE)
    narrative = (await _lock_pair(session, [narrative_id]))[narrative_id]
    _require_open(narrative)
    wanted = list(dict.fromkeys(group_ids))
    if not wanted:
        raise InvalidInput(
            "No group to split", code=ec.FCK_NARRATIVE_INVALID, narrative=str(narrative_id)
        )
    groups = list(
        (
            await session.scalars(
                select(ClaimGroup).where(ClaimGroup.public_id.in_(wanted)).order_by(ClaimGroup.id)
            )
        ).all()
    )
    members = set(await active_group_ids(session, narrative.id))
    moved_ids = [g.id for g in groups]
    if (
        len(groups) != len(wanted)
        or not set(moved_ids) <= members
        or narrative.founder_group_id in moved_ids
        or len(moved_ids) >= len(members)
    ):
        raise InvalidInput(
            "Groups must be members of the narrative, not its founder, and one group must stay",
            code=ec.FCK_NARRATIVE_INVALID,
            narrative=str(narrative_id),
        )
    now = datetime.now(UTC)
    await session.execute(
        update(NarrativeGroup)
        .where(
            NarrativeGroup.narrative_id == narrative.id,
            NarrativeGroup.group_id.in_(moved_ids),
            NarrativeGroup.removed_at.is_(None),
        )
        .values(removed_at=func.now())
    )
    await session.flush()
    founder = max(groups, key=lambda g: (g.reach, -g.id))
    created = await create_narrative(
        session, actor, group=founder, signals={"split_from": str(narrative.public_id)}
    )
    for group in groups:
        if group.id != founder.id:
            session.add(
                NarrativeGroup(
                    narrative_id=created.id,
                    group_id=group.id,
                    signals={"split_from": str(narrative.public_id)},
                    reason="split",
                )
            )
    narrative.split_suggested = False
    narrative.updated_at = now
    await session.flush()
    await elect_narrative_medoids(session, actor, created, force=True)
    await _refresh_medoids_after_removal(session, actor, narrative, moved_ids)
    for source_side, other in ((narrative, created), (created, narrative)):
        await _journal(
            session,
            actor,
            source_side,
            NarrativeEventKind.SPLIT,
            other=other,
            group_ids=moved_ids,
            reason=reason,
        )
    await publish(
        session,
        names.NARRATIVE_SPLIT,
        {
            "narrative_id": str(narrative.public_id),
            "new_narrative_id": str(created.public_id),
            "group_ids": [str(g.public_id) for g in groups],
            "actor": actor.label,
            "reason": reason,
        },
        aggregate_type="narrative",
        aggregate_id=str(narrative.public_id),
        actor=actor.label,
    )
    log.info("narrative.split", narrative=narrative.id, new=created.id, groups=len(moved_ids))
    return created


# ------------------------------------------------------------------ wave mode quota


async def group_in_surge(session: AsyncSession, group_id: int) -> bool:
    """True when the group belongs to a narrative at ``surge`` or ``alert``."""
    found = await session.scalar(
        select(Narrative.id)
        .join(NarrativeGroup, NarrativeGroup.narrative_id == Narrative.id)
        .where(
            NarrativeGroup.group_id == group_id,
            NarrativeGroup.removed_at.is_(None),
            Narrative.state.in_(WAVE_STATES),
        )
        .limit(1)
    )
    return found is not None


async def surge_drafts_today(session: AsyncSession, now: datetime | None = None) -> int:
    """Drafts opened since 00:00 UTC with ``flags.surge`` (the separate wave quota)."""
    now = now or datetime.now(UTC)
    start = datetime(now.year, now.month, now.day, tzinfo=UTC)
    return int(
        await session.scalar(
            text(
                "SELECT count(*) FROM verdicts "
                "WHERE drafted_at >= :start AND flags->>'surge' = 'true'"
            ),
            {"start": start},
        )
        or 0
    )
