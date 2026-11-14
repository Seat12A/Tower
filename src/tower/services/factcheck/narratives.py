"""Narratives and waves (lot 5): attachment of new groups, maintenance, window stats and
tiers, wave mode effects, surge sampling and novelty, narrative titles.

Rules: docs/factcheck/vagues.md and CONTRACT5 decisions 8-11. Grouping never uses the
author's identity (accounts are only counted). When in doubt, a group does not attach.
"""

from __future__ import annotations

import math
import random
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Float, delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.config import Settings
from tower.core.domain import error_codes as ec
from tower.core.domain.factcheck.datamark import datamark
from tower.core.domain.factcheck.values import (
    UNVERIFIABLE_CLAIM_TYPES,
    ClaimGroupState,
    NarrativeState,
    TitleStatus,
)
from tower.core.domain.factcheck.waves import (
    NOVELTY_PERCENTILE_MIN_SAMPLES,
    SPECIFIC_MEDIA_MAX_NARRATIVES,
    WAVE_STATES,
    AttachAnchors,
    AttachSignals,
    WaveThresholds,
    WindowStats,
    cooccurrence_met,
    engagement_of,
    entity_key,
    evaluate_attachment,
    expected_count,
    extract_links,
    find_bridge,
    growth,
    next_state,
    novelty_fingerprints,
    novelty_takes,
    parse_thresholds,
    percentile,
    poisson_score,
    sampling_rate,
    stratified_sample,
    tier_of,
)
from tower.core.domain.llm import Level
from tower.core.domain.security import Actor
from tower.core.errors import capture
from tower.core.events.outbox import EventRecord
from tower.core.factcheck import inputs, names
from tower.core.factcheck.candidates import nearest_groups
from tower.core.factcheck.profiles import active_profile
from tower.core.factcheck.schemas import NarrativeTitleOutput
from tower.core.jobs.queue import PermanentError, enqueue
from tower.core.llm.errors import AllRefused
from tower.core.logging import get_logger
from tower.core.models.factcheck import (
    Claim,
    ClaimGroup,
    ContentClaim,
    EvidencePassage,
    Investigation,
)
from tower.core.models.factcheck_v2 import (
    EntityRarity,
    Narrative,
    NarrativeGroup,
    NarrativeStat,
)
from tower.core.models.ingestion import ContentItem
from tower.core.ports.llm import LLMRequest
from tower.core.services.base import JobContext
from tower.core.usecases import claims as claims_uc
from tower.core.usecases import narratives as nar

log = get_logger(__name__)

QUEUE = names.FC_BULK
NEAREST_GROUPS = 10
MAX_NARRATIVES_PER_PASS = 500
SAMPLING_COSINE = 0.85
MAX_SURGING_NARRATIVES = 20
RARITY_WINDOW_DAYS = 30
STATS_RETENTION_DAYS = 30
RARITY_RETENTION_DAYS = 31
FILTER_FREEZE_HOURS = 24
MAX_SPLIT_GRAPH_GROUPS = 200
MAX_SPLIT_CHECKS = 100
LIGHT_CHECKS_PER_NARRATIVE = 50
SPANS: tuple[tuple[str, timedelta], ...] = (
    ("5m", timedelta(minutes=5)),
    ("1h", timedelta(hours=1)),
    ("6h", timedelta(hours=6)),
)
_TITLE_CLAIMS = 5
_TITLE_MAX_TOKENS = 300


def _actor() -> Actor:
    return Actor.system(names.FACTCHECK_ACTOR)


# ------------------------------------------------------------------- subscription


async def on_group_created(session: AsyncSession, events: Sequence[EventRecord]) -> None:
    """Subscription on ``claim_group.created``: enqueue ``factcheck.attach_narrative``."""
    public_ids: list[uuid.UUID] = []
    for event in events:
        try:
            public_ids.append(uuid.UUID(str(event.payload["group_id"])))
        except (KeyError, ValueError):
            continue
    if not public_ids:
        return
    rows = await session.execute(
        select(ClaimGroup.id, ClaimGroup.public_id)
        .where(ClaimGroup.public_id.in_(public_ids))
        .order_by(ClaimGroup.id)
    )
    for group_id, public_id in rows.all():
        await enqueue(
            session,
            QUEUE,
            names.JOB_ATTACH_NARRATIVE,
            {"group_id": str(public_id), "group_row_id": int(group_id)},
            dedupe_key=names.attach_key(int(group_id)),
        )


# --------------------------------------------------------------------- attachment


@dataclass(slots=True)
class _GroupFacts:
    entities: set[str] = field(default_factory=set)
    media: set[int] = field(default_factory=set)
    links: set[str] = field(default_factory=set)


@dataclass(slots=True)
class _Candidate:
    narrative: Narrative
    signals: AttachSignals
    anchors: AttachAnchors
    similarity: float
    detail: dict[str, Any]


async def _entity_keys(
    session: AsyncSession, narrative_id: int | None, group_id: int | None
) -> set[str]:
    """Entity keys of the claims of one group, or of every active group of a narrative."""
    params: dict[str, Any]
    if group_id is not None:
        where, params = "m.group_id = :g", {"g": group_id}
        join = ""
    else:
        where, params = "ng.narrative_id = :n AND ng.removed_at IS NULL", {"n": narrative_id}
        join = "JOIN narrative_groups ng ON ng.group_id = m.group_id"
    rows = await session.execute(
        text(
            "SELECT DISTINCT e->>'name', e->>'qid' FROM claim_group_members m "  # noqa: S608
            f"{join} JOIN claims c ON c.id = m.claim_id, "
            "jsonb_array_elements(c.entities) e "
            f"WHERE m.removed_at IS NULL AND {where} LIMIT 500"
        ),
        params,
    )
    return {entity_key(str(name or ""), qid) for name, qid in rows.all() if name or qid}


_GROUP_CONTENTS = (
    "SELECT DISTINCT cc.content_id FROM claim_group_members m "
    "JOIN content_claims cc ON cc.claim_id = m.claim_id "
    "WHERE m.group_id = :g AND m.removed_at IS NULL"
)


async def _group_facts(session: AsyncSession, group_id: int) -> _GroupFacts:
    facts = _GroupFacts(entities=await _entity_keys(session, None, group_id))
    media = await session.execute(
        text(
            f"SELECT DISTINCT cm.media_id FROM ({_GROUP_CONTENTS}) gc "  # noqa: S608
            "JOIN content_media cm ON cm.content_id = gc.content_id LIMIT 200"
        ),
        {"g": group_id},
    )
    facts.media = {int(r[0]) for r in media.all()}
    texts = await session.execute(
        text(
            f"SELECT ci.text FROM ({_GROUP_CONTENTS}) gc "  # noqa: S608
            "JOIN content_items ci ON ci.id = gc.content_id ORDER BY ci.id DESC LIMIT 50"
        ),
        {"g": group_id},
    )
    for (body,) in texts.all():
        facts.links |= extract_links(str(body))
    return facts


async def _medoid_similarity(
    session: AsyncSession, pivot: Sequence[float], medoid_ids: Sequence[int]
) -> float:
    if not medoid_ids:
        return 0.0
    distance = ClaimGroup.representative_embedding.op("<=>", return_type=Float)(list(pivot))
    value = await session.scalar(
        select(func.max(1 - distance)).where(
            ClaimGroup.id.in_(list(medoid_ids)), ClaimGroup.representative_embedding.is_not(None)
        )
    )
    return float(value) if value is not None else 0.0


async def _evaluate(
    session: AsyncSession,
    settings: Settings,
    group: ClaimGroup,
    facts: _GroupFacts,
    narrative: Narrative,
    similarity: float,
    today: date,
) -> _Candidate:
    narrative_entities = await _entity_keys(session, narrative.id, None)
    shared_entities = facts.entities & narrative_entities

    shared_media: set[int] = set()
    if facts.media:
        rows = await session.execute(
            text(
                f"SELECT DISTINCT cm.media_id FROM ({nar.NARRATIVE_CONTENTS}) nc "  # noqa: S608
                "JOIN content_media cm ON cm.content_id = nc.content_id "
                "WHERE cm.media_id = ANY(:media)"
            ),
            {"nids": [narrative.id], "media": sorted(facts.media)},
        )
        shared_media = {int(r[0]) for r in rows.all()}
    specific_media = False
    if shared_media:
        rows = await session.execute(
            text(
                "SELECT cm.media_id, count(DISTINCT ng.narrative_id) FROM content_media cm "
                "JOIN content_claims cc ON cc.content_id = cm.content_id "
                "JOIN claim_group_members m ON m.claim_id = cc.claim_id AND m.removed_at IS NULL "
                "JOIN narrative_groups ng ON ng.group_id = m.group_id AND ng.removed_at IS NULL "
                "WHERE cm.media_id = ANY(:ids) GROUP BY cm.media_id"
            ),
            {"ids": sorted(shared_media)},
        )
        specific_media = any(int(n) <= SPECIFIC_MEDIA_MAX_NARRATIVES for _, n in rows.all())

    shared_links: set[str] = set()
    if facts.links:
        rows = await session.execute(
            text(
                f"SELECT ci.text FROM ({nar.NARRATIVE_CONTENTS}) nc "  # noqa: S608
                "JOIN content_items ci ON ci.id = nc.content_id ORDER BY ci.id DESC LIMIT 100"
            ),
            {"nids": [narrative.id]},
        )
        links: set[str] = set()
        for (body,) in rows.all():
            links |= extract_links(str(body))
        shared_links = facts.links & links

    rare_entity = False
    if shared_entities:
        rows = await session.execute(
            select(EntityRarity.entity_key, func.sum(EntityRarity.count))
            .where(
                EntityRarity.entity_key.in_(sorted(shared_entities)),
                EntityRarity.day >= today - timedelta(days=RARITY_WINDOW_DAYS),
            )
            .group_by(EntityRarity.entity_key)
        )
        seen = {k: int(v or 0) for k, v in rows.all()}
        rare_entity = any(
            seen.get(key, 0) <= settings.fc_narrative_rare_entity_max for key in shared_entities
        )

    co = (
        await session.execute(
            text(
                "SELECT count(DISTINCT cc1.content_id), "
                "count(DISTINCT ci.author_account_id) "
                "FROM claim_group_members m1 "
                "JOIN content_claims cc1 ON cc1.claim_id = m1.claim_id "
                "JOIN content_claims cc2 ON cc2.content_id = cc1.content_id "
                "JOIN claim_group_members m2 ON m2.claim_id = cc2.claim_id "
                "AND m2.removed_at IS NULL "
                "JOIN narrative_groups ng ON ng.group_id = m2.group_id "
                "AND ng.removed_at IS NULL AND ng.narrative_id = :n "
                "JOIN content_items ci ON ci.id = cc1.content_id "
                "WHERE m1.group_id = :g AND m1.removed_at IS NULL"
            ),
            {"g": group.id, "n": narrative.id},
        )
    ).one()
    signals = AttachSignals(
        semantic=similarity >= settings.fc_narrative_semantic_threshold,
        cooccurrence=cooccurrence_met(int(co[0]), int(co[1])),
        entity=bool(shared_entities),
        media=bool(shared_media),
    )
    anchors = AttachAnchors(
        rare_entity=rare_entity, specific_media=specific_media, shared_link=bool(shared_links)
    )
    return _Candidate(
        narrative=narrative,
        signals=signals,
        anchors=anchors,
        similarity=similarity,
        detail={
            "signals": signals.as_dict(),
            "anchors": anchors.as_dict(),
            "medoid_similarity": round(similarity, 4),
            "cooccurrence_posts": int(co[0]),
            "cooccurrence_accounts": int(co[1]),
        },
    )


async def _bump_rarity(session: AsyncSession, keys: set[str], today: date) -> None:
    if not keys:
        return
    stmt = pg_insert(EntityRarity).values(
        [{"entity_key": k, "day": today, "count": 1} for k in sorted(keys)]
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[EntityRarity.entity_key, EntityRarity.day],
            set_={"count": EntityRarity.count + 1},
        )
    )


async def attach_narrative(ctx: JobContext) -> None:
    """Job ``factcheck.attach_narrative``: attach the group to the best matching open
    narrative, or found a new one (a match only on a closed narrative founds a new one
    that points to it). Idempotent: a group already in a narrative is left alone."""
    try:
        public_id = uuid.UUID(str(ctx.job.payload["group_id"]))
    except (KeyError, ValueError) as exc:
        raise PermanentError("bad attach_narrative payload") from exc
    settings = ctx.settings
    actor = _actor()
    today = datetime.now(UTC).date()
    async with ctx.db.transaction() as session:
        group = await session.scalar(
            select(ClaimGroup).where(ClaimGroup.public_id == public_id).with_for_update()
        )
        if group is None or group.state == ClaimGroupState.MERGED.value:
            log.info("narrative.attach_skipped", group=str(public_id))
            return
        already = await session.scalar(
            select(NarrativeGroup.group_id)
            .where(NarrativeGroup.group_id == group.id, NarrativeGroup.removed_at.is_(None))
            .limit(1)
        )
        if already is not None:
            return
        facts = await _group_facts(session, group.id)
        candidates: list[_Candidate] = []
        if group.representative_embedding is not None:
            near = await nearest_groups(
                session, group.representative_embedding, limit=NEAREST_GROUPS
            )
            ids = [r.group_id for r in near if r.group_id != group.id]
            narratives = (
                list(
                    (
                        await session.scalars(
                            select(Narrative)
                            .join(NarrativeGroup, NarrativeGroup.narrative_id == Narrative.id)
                            .where(
                                NarrativeGroup.group_id.in_(ids),
                                NarrativeGroup.removed_at.is_(None),
                                Narrative.merged_into_id.is_(None),
                            )
                            .distinct()
                            .order_by(Narrative.id)
                        )
                    ).all()
                )
                if ids
                else []
            )
            for narrative in narratives:
                similarity = await _medoid_similarity(
                    session, group.representative_embedding, narrative.medoid_group_ids
                )
                candidate = await _evaluate(
                    session, settings, group, facts, narrative, similarity, today
                )
                if evaluate_attachment(
                    candidate.signals,
                    candidate.anchors,
                    medoid_similarity=similarity,
                    medoid_floor=settings.fc_narrative_medoid_floor,
                ):
                    candidates.append(candidate)

        def best(items: list[_Candidate]) -> _Candidate | None:
            return max(items, key=lambda c: (c.signals.count(), c.similarity), default=None)

        chosen = best([c for c in candidates if c.narrative.state != NarrativeState.CLOSED.value])
        if chosen is not None:
            await nar.attach_group(
                session, actor, chosen.narrative, group, signals=chosen.detail, reason="match"
            )
            log.info("narrative.attached", narrative=chosen.narrative.id, group=group.id)
        else:
            closed = best(
                [c for c in candidates if c.narrative.state == NarrativeState.CLOSED.value]
            )
            await nar.create_narrative(
                session,
                actor,
                group=group,
                previous=closed.narrative if closed is not None else None,
                signals=closed.detail if closed is not None else None,
            )
        await _bump_rarity(session, facts.entities, today)


# ------------------------------------------------------------------------- title


async def narrative_title(ctx: JobContext) -> None:
    """Job ``factcheck.narrative_title``: draft title (French) from up to 5 representative
    claims of the medoid groups; a reviewer validates it later."""
    try:
        narrative_id = int(ctx.job.payload["narrative_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PermanentError("bad narrative_title payload") from exc
    from tower.services.factcheck.jobs import _complete

    async with ctx.db.session() as session:
        narrative = await session.get(Narrative, narrative_id)
        if narrative is None or narrative.title_status == TitleStatus.VALIDATED.value:
            return
        medoids = list(narrative.medoid_group_ids)
        rows = await session.execute(
            select(ClaimGroup.id, Claim.text_fr)
            .join(Claim, Claim.id == ClaimGroup.representative_claim_id)
            .join(NarrativeGroup, NarrativeGroup.group_id == ClaimGroup.id)
            .where(NarrativeGroup.narrative_id == narrative_id, NarrativeGroup.removed_at.is_(None))
            .order_by(ClaimGroup.reach.desc(), ClaimGroup.id)
        )
        by_id = {int(g): str(t) for g, t in rows.all()}
    ordered = [by_id[g] for g in medoids if g in by_id]
    ordered += [t for g, t in by_id.items() if g not in medoids]
    claims = [datamark(t) for t in ordered[:_TITLE_CLAIMS]]
    if not claims:
        return
    request = LLMRequest(
        role=names.ROLE_NARRATIVE_TITLE,
        level=Level.N1,
        prompt_id=names.prompt_id(names.ROLE_NARRATIVE_TITLE),
        input=inputs.narrative_title_input(claims=claims),
        schema=NarrativeTitleOutput,
        max_output_tokens=_TITLE_MAX_TOKENS,
        budget_scope=names.level_scope(Level.N1.value),
    )
    try:
        result = await _complete(ctx, request)
    except AllRefused as exc:
        capture(ec.FCK_MODEL_REFUSED, exc, stage="narrative_title", narrative=narrative_id)
        return
    output = result.output
    if not isinstance(output, NarrativeTitleOutput):
        raise TypeError("unexpected narrative title output type")
    async with ctx.db.transaction() as session:
        locked = await session.scalar(
            select(Narrative).where(Narrative.id == narrative_id).with_for_update()
        )
        if locked is not None and await nar.set_title(
            session, _actor(), locked, title_fr=output.title_fr, title_en=output.title_en
        ):
            log.info("narrative.title_drafted", narrative=narrative_id)


# -------------------------------------------------------------------------- waves


def _thresholds(settings: Settings) -> WaveThresholds:
    try:
        return parse_thresholds(settings.fc_wave_thresholds)
    except ValueError:
        log.warning("waves.bad_thresholds")
        return WaveThresholds()


async def _window_posts(
    session: AsyncSession, nids: Sequence[int], start: datetime, end: datetime
) -> dict[int, tuple[int, int, int, int]]:
    """``{narrative_id: (posts, accounts, platforms, reach)}`` over ``(start, end]``."""
    params = {"nids": list(nids), "start": start, "end": end}
    rows = await session.execute(
        text(
            f"WITH nc AS ({nar.NARRATIVE_CONTENTS}) "  # noqa: S608
            "SELECT nc.narrative_id, count(*), count(DISTINCT ci.author_account_id), "
            "count(DISTINCT o.platform) FROM nc "
            "JOIN observations o ON o.content_id = nc.content_id "
            "AND o.observed_at > :start AND o.observed_at <= :end "
            "JOIN content_items ci ON ci.id = nc.content_id GROUP BY nc.narrative_id"
        ),
        params,
    )
    out = {int(r[0]): (int(r[1]), int(r[2]), int(r[3]), 0) for r in rows.all()}
    reach = await session.execute(
        text(
            f"WITH nc AS ({nar.NARRATIVE_CONTENTS}) "  # noqa: S608
            "SELECT nc.narrative_id, COALESCE(sum(CASE WHEN "
            "jsonb_typeof(ci.metrics->'impressions') = 'number' "
            "THEN (ci.metrics->>'impressions')::numeric ELSE 0 END), 0)::bigint "
            "FROM nc JOIN content_items ci ON ci.id = nc.content_id "
            "WHERE EXISTS (SELECT 1 FROM observations o WHERE o.content_id = nc.content_id "
            "AND o.observed_at > :start AND o.observed_at <= :end) GROUP BY nc.narrative_id"
        ),
        params,
    )
    for nid, value in reach.all():
        if int(nid) in out:
            posts, accounts, platforms, _ = out[int(nid)]
            out[int(nid)] = (posts, accounts, platforms, int(value))
    return out


async def _flow(session: AsyncSession, start: datetime, end: datetime) -> int:
    return int(
        await session.scalar(
            text("SELECT count(*) FROM observations WHERE observed_at > :s AND observed_at <= :e"),
            {"s": start, "e": end},
        )
        or 0
    )


async def _platform_view(
    session: AsyncSession, nids: Sequence[int], start: datetime, end: datetime
) -> tuple[dict[int, dict[str, tuple[int, int]]], dict[str, int]]:
    """Per narrative and platform ``(posts, accounts)``, and the flow of each platform."""
    params = {"nids": list(nids), "start": start, "end": end}
    rows = await session.execute(
        text(
            f"WITH nc AS ({nar.NARRATIVE_CONTENTS}) "  # noqa: S608
            "SELECT nc.narrative_id, o.platform, count(*), count(DISTINCT ci.author_account_id) "
            "FROM nc JOIN observations o ON o.content_id = nc.content_id "
            "AND o.observed_at > :start AND o.observed_at <= :end "
            "JOIN content_items ci ON ci.id = nc.content_id GROUP BY nc.narrative_id, o.platform"
        ),
        params,
    )
    per: dict[int, dict[str, tuple[int, int]]] = {}
    for nid, platform, posts, accounts in rows.all():
        per.setdefault(int(nid), {})[str(platform)] = (int(posts), int(accounts))
    flows = await session.execute(
        text(
            "SELECT platform, count(*) FROM observations "
            "WHERE observed_at > :start AND observed_at <= :end GROUP BY platform"
        ),
        {"start": start, "end": end},
    )
    return per, {str(p): int(c) for p, c in flows.all()}


async def _candidate_narratives(session: AsyncSession, now: datetime) -> list[int]:
    """Narratives with activity in the last 6 h (most active first, bounded), plus every
    narrative still above the active state (so that decay is noticed)."""
    rows = await session.execute(
        text(
            "WITH recent AS (SELECT content_id FROM observations "
            "WHERE observed_at > :start AND observed_at <= :end GROUP BY content_id) "
            "SELECT ng.narrative_id, count(DISTINCT r.content_id) AS posts FROM recent r "
            "JOIN content_items rc ON rc.id = r.content_id "
            "JOIN content_claims cc ON cc.content_id = coalesce(rc.copy_of_id, rc.id) "
            "JOIN claim_group_members m ON m.claim_id = cc.claim_id AND m.removed_at IS NULL "
            "JOIN narrative_groups ng ON ng.group_id = m.group_id AND ng.removed_at IS NULL "
            "JOIN narratives n ON n.id = ng.narrative_id AND n.state <> 'closed' "
            "AND n.merged_into_id IS NULL "
            "GROUP BY ng.narrative_id ORDER BY posts DESC, ng.narrative_id LIMIT :limit"
        ),
        {"start": now - timedelta(hours=6), "end": now, "limit": MAX_NARRATIVES_PER_PASS},
    )
    ids = [int(r[0]) for r in rows.all()]
    raised = await session.scalars(
        select(Narrative.id)
        .where(
            Narrative.state.in_(
                (NarrativeState.WATCH.value, NarrativeState.SURGE.value, NarrativeState.ALERT.value)
            ),
            Narrative.merged_into_id.is_(None),
        )
        .order_by(Narrative.id)
        .limit(MAX_NARRATIVES_PER_PASS)
    )
    for nid in raised.all():
        if int(nid) not in ids:
            ids.append(int(nid))
    return ids


async def _choose_representative(session: AsyncSession, narrative: Narrative) -> ClaimGroup | None:
    """Highest reach, then a claim with a value (verifiable claims first)."""
    group = await session.scalar(
        select(ClaimGroup)
        .join(NarrativeGroup, NarrativeGroup.group_id == ClaimGroup.id)
        .join(Claim, Claim.id == ClaimGroup.representative_claim_id)
        .where(
            NarrativeGroup.narrative_id == narrative.id,
            NarrativeGroup.removed_at.is_(None),
            ClaimGroup.state != ClaimGroupState.MERGED.value,
        )
        .order_by(
            Claim.claim_type.in_([t.value for t in UNVERIFIABLE_CLAIM_TYPES]),
            ClaimGroup.reach.desc(),
            Claim.value_low.is_(None),
            ClaimGroup.id,
        )
        .limit(1)
    )
    return group


async def _latest_content(session: AsyncSession, group: ClaimGroup) -> ContentItem | None:
    return await session.scalar(
        select(ContentItem)
        .join(ContentClaim, ContentClaim.content_id == ContentItem.id)
        .where(ContentClaim.claim_id == group.representative_claim_id)
        .order_by(ContentItem.id.desc())
        .limit(1)
    )


async def _enter_wave(session: AsyncSession, narrative: Narrative) -> None:
    """Before the state change: pick the representative group (kept once chosen)."""
    current = narrative.representative_group_id
    if current is not None:
        member = await session.scalar(
            select(NarrativeGroup.group_id)
            .where(
                NarrativeGroup.narrative_id == narrative.id,
                NarrativeGroup.group_id == current,
                NarrativeGroup.removed_at.is_(None),
            )
            .limit(1)
        )
        if member is not None:
            return
    chosen = await _choose_representative(session, narrative)
    narrative.representative_group_id = chosen.id if chosen is not None else None
    await session.flush()


async def _wave_effects(
    session: AsyncSession, narrative: Narrative, tier: str, counters: dict[str, int]
) -> None:
    """After the state change: groups at N2 (alert: N4), one investigation of the
    representative group (never a second one)."""
    actor = _actor()
    level = "N4" if tier == NarrativeState.ALERT.value else "N2"
    groups = list(
        (
            await session.scalars(
                select(ClaimGroup)
                .join(NarrativeGroup, NarrativeGroup.group_id == ClaimGroup.id)
                .where(
                    NarrativeGroup.narrative_id == narrative.id,
                    NarrativeGroup.removed_at.is_(None),
                    ClaimGroup.state != ClaimGroupState.MERGED.value,
                )
                .order_by(ClaimGroup.id)
            )
        ).all()
    )
    for group in groups:
        if await claims_uc.raise_group_level(session, actor, group, level, reason="wave"):
            counters["groups_raised"] += 1
    representative = next((g for g in groups if g.id == narrative.representative_group_id), None)
    if representative is None:
        return
    claim_type = await session.scalar(
        select(Claim.claim_type).where(Claim.id == representative.representative_claim_id)
    )
    if claim_type in {t.value for t in UNVERIFIABLE_CLAIM_TYPES}:
        return
    content = await _latest_content(session, representative)
    if content is None:
        return
    existing = await session.scalar(
        select(Investigation.id)
        .where(Investigation.claim_group_id == representative.id, Investigation.stage != "failed")
        .limit(1)
    )
    started = None
    if level == "N4":
        started = await claims_uc.promote_to_n4(
            session,
            actor,
            group=representative,
            content=content,
            requested_by=names.FACTCHECK_ACTOR,
        )
    elif existing is None:
        # The investigation queue has no priority parameter yet: the "high priority" of
        # the wave mode is carried by the investigation flags only.
        started = await claims_uc.request_investigation(
            session,
            actor,
            group=representative,
            content=content,
            level=level,
            requested_by=names.FACTCHECK_ACTOR,
            flags={"wave": True, "narrative_id": str(narrative.public_id)},
        )
    if started is not None:
        counters["investigations"] += 1
        if representative.state == ClaimGroupState.DORMANT.value:
            representative.state = ClaimGroupState.QUEUED.value
            representative.updated_at = func.now()
        await session.flush()


async def _light_checks(
    session: AsyncSession, settings: Settings, narrative: Narrative, counters: dict[str, int]
) -> None:
    """Groups of a surging narrative are judged on the dossier of the representative
    investigation once it holds kept passages (and again if the dossier is newer than
    their last check)."""
    representative = narrative.representative_group_id
    if representative is None:
        return
    investigation = await session.scalar(
        select(Investigation)
        .where(
            Investigation.claim_group_id == representative,
            Investigation.stage.in_(("completed", "assembled")),
        )
        .order_by(Investigation.id.desc())
        .limit(1)
    )
    if investigation is None:
        return
    kept = await session.scalar(
        select(func.count())
        .select_from(EvidencePassage)
        .where(EvidencePassage.investigation_id == investigation.id, EvidencePassage.kept.is_(True))
    )
    if not kept:
        return
    ready_at = investigation.completed_at or investigation.updated_at
    rows = await session.scalars(
        select(ClaimGroup.id)
        .join(NarrativeGroup, NarrativeGroup.group_id == ClaimGroup.id)
        .where(
            NarrativeGroup.narrative_id == narrative.id,
            NarrativeGroup.removed_at.is_(None),
            ClaimGroup.state != ClaimGroupState.MERGED.value,
            ClaimGroup.id != representative,
            (ClaimGroup.light_checked_at.is_(None)) | (ClaimGroup.light_checked_at < ready_at),
        )
        .order_by(ClaimGroup.reach.desc(), ClaimGroup.id)
        .limit(LIGHT_CHECKS_PER_NARRATIVE)
    )
    version = (await active_profile(session, settings)).version
    for group_id in rows.all():
        queued = await enqueue(
            session,
            QUEUE,
            names.JOB_LIGHT_CHECK,
            {"group_id": int(group_id), "dossier_investigation_id": investigation.id},
            dedupe_key=names.light_check_key(int(group_id), version),
        )
        if queued is not None:
            counters["light_checks"] += 1


async def waves_step(session: AsyncSession, settings: Settings, now: datetime) -> dict[str, Any]:
    """Called by the scheduler pass: window stats, tiers, wave mode effects.

    At most 500 narratives per pass; every figure is an SQL aggregate. Returns counts."""
    counters: dict[str, int] = {
        "narratives": 0,
        "stats_rows": 0,
        "watch": 0,
        "surge_entered": 0,
        "alert_entered": 0,
        "surge_ended": 0,
        "investigations": 0,
        "groups_raised": 0,
        "light_checks": 0,
        "titles": 0,
    }
    nids = await _candidate_narratives(session, now)
    if not nids:
        return dict(counters)
    thresholds = _thresholds(settings)
    watched = {t.strip() for t in settings.fc_watched_themes.split(",") if t.strip()}
    newest_filter = await session.scalar(
        text("SELECT max(created_at) FROM extension_filter_versions")
    )
    frozen = newest_filter is not None and now - newest_filter < timedelta(
        hours=FILTER_FREEZE_HOURS
    )

    current: dict[str, dict[int, tuple[int, int, int, int]]] = {}
    previous: dict[str, dict[int, tuple[int, int, int, int]]] = {}
    flows: dict[str, tuple[int, int]] = {}
    for span, length in SPANS:
        current[span] = await _window_posts(session, nids, now - length, now)
        previous[span] = await _window_posts(session, nids, now - 2 * length, now - length)
        flows[span] = (
            await _flow(session, now - length, now),
            await _flow(session, now - 2 * length, now - length),
        )
    per_platform, platform_flow = await _platform_view(session, nids, now - timedelta(hours=1), now)
    n3 = {
        int(r)
        for r in (
            await session.scalars(
                select(NarrativeGroup.narrative_id)
                .join(ClaimGroup, ClaimGroup.id == NarrativeGroup.group_id)
                .where(
                    NarrativeGroup.narrative_id.in_(nids),
                    NarrativeGroup.removed_at.is_(None),
                    ClaimGroup.level.in_(("N3", "N4")),
                )
                .distinct()
            )
        ).all()
    }

    rows = list(
        (
            await session.scalars(
                select(Narrative)
                .where(Narrative.id.in_(nids), Narrative.state != NarrativeState.CLOSED.value)
                .order_by(Narrative.id)
                .with_for_update()
            )
        ).all()
    )
    actor = _actor()
    zero = (0, 0, 0, 0)
    for narrative in rows:
        counters["narratives"] += 1
        stat_rows: list[dict[str, Any]] = []
        for span, _length in SPANS:
            posts, accounts, platforms, reach = current[span].get(narrative.id, zero)
            prev_posts = previous[span].get(narrative.id, zero)[0]
            flow, prev_flow = flows[span]
            if posts == 0 and prev_posts == 0:
                continue
            stat_rows.append(
                {
                    "narrative_id": narrative.id,
                    "span": span,
                    "window_end": now,
                    "posts": posts,
                    "flow_posts": flow,
                    "share": posts / flow if flow > 0 else 0.0,
                    "accounts": accounts,
                    "platforms": platforms,
                    "reach": reach,
                    "score": poisson_score(posts, expected_count(prev_posts, flow, prev_flow)),
                }
            )
        if stat_rows:
            stmt = pg_insert(NarrativeStat).values(stat_rows)
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[
                        NarrativeStat.narrative_id,
                        NarrativeStat.span,
                        NarrativeStat.window_end,
                    ],
                    set_={
                        c: stmt.excluded[c]
                        for c in stat_rows[0]
                        if c not in ("narrative_id", "span", "window_end")
                    },
                )
            )
            counters["stats_rows"] += len(stat_rows)
        if current["6h"].get(narrative.id, zero)[0] > 0:
            narrative.last_activity_at = now

        posts_1h, accounts_1h, _, _ = current["1h"].get(narrative.id, zero)
        flow_1h = flows["1h"][0]
        platforms_view = per_platform.get(narrative.id, {})
        at_watch = sum(
            1
            for platform, (p_posts, p_accounts) in platforms_view.items()
            if platform_flow.get(platform, 0) > 0
            and p_posts / platform_flow[platform] >= thresholds.watch_share
            and p_accounts >= thresholds.watch_accounts
        )
        stats = WindowStats(
            posts=posts_1h,
            flow=flow_1h,
            accounts=accounts_1h,
            platforms_at_watch=at_watch,
            growth=growth(posts_1h, previous["1h"].get(narrative.id, zero)[0]),
        )
        computed = tier_of(
            stats,
            thresholds,
            has_n3_group=narrative.id in n3,
            watched_theme=narrative.theme in watched if narrative.theme else False,
            growth_frozen=frozen,
        )
        old_state = narrative.state
        new_state, below = next_state(
            old_state,
            computed,
            narrative.below_watch_since,
            now,
            end_hours=settings.fc_surge_end_hours,
        )
        report_stats = {
            "share": round(stats.share, 6),
            "accounts": accounts_1h,
            "platforms": sorted(platforms_view),
            "growth": round(stats.growth, 3),
            "posts": posts_1h,
        }
        if new_state != old_state:
            if new_state in WAVE_STATES:
                await _enter_wave(session, narrative)
            await nar.change_state(
                session, actor, narrative, new_state, reason="wave", stats=report_stats, now=now
            )
            if new_state in WAVE_STATES:
                counters["surge_entered" if new_state == "surge" else "alert_entered"] += 1
                await _wave_effects(session, narrative, new_state, counters)
            elif old_state in WAVE_STATES:
                counters["surge_ended"] += 1
            elif new_state == NarrativeState.WATCH.value:
                counters["watch"] += 1
        await nar.set_below_watch(session, narrative, below)
        if narrative.state in WAVE_STATES:
            await _light_checks(session, settings, narrative, counters)
        if (
            narrative.state != NarrativeState.ACTIVE.value
            and narrative.title_status == TitleStatus.NONE.value
        ):
            queued = await enqueue(
                session,
                QUEUE,
                names.JOB_NARRATIVE_TITLE,
                {"narrative_id": narrative.id},
                dedupe_key=names.title_key(narrative.id),
            )
            if queued is not None:
                counters["titles"] += 1
    await session.flush()
    return dict(counters)


# ---------------------------------------------------------------------- maintenance


async def maintain_narratives(session: AsyncSession, settings: Settings) -> int:
    """Periodic ``factcheck:narratives``: closure after ``fc_narrative_close_days`` of
    inactivity, daily medoid re-election, split flags, purge of old stats and rarity.
    Returns the number of changes."""
    now = datetime.now(UTC)
    actor = _actor()
    changes = 0
    open_filter = (
        Narrative.state != NarrativeState.CLOSED.value,
        Narrative.merged_into_id.is_(None),
    )

    stale = await session.scalars(
        select(Narrative)
        .where(
            *open_filter,
            Narrative.last_activity_at < now - timedelta(days=settings.fc_narrative_close_days),
        )
        .order_by(Narrative.id)
        .limit(MAX_NARRATIVES_PER_PASS)
        .with_for_update(skip_locked=True)
    )
    for narrative in stale.all():
        if await nar.close_narrative(session, actor, narrative, reason="inactivity", now=now):
            changes += 1

    due = await session.scalars(
        select(Narrative)
        .where(
            *open_filter,
            (Narrative.medoids_elected_at.is_(None))
            | (Narrative.medoids_elected_at < now - nar.MEDOID_ELECTION_INTERVAL),
        )
        .order_by(Narrative.id)
        .limit(MAX_NARRATIVES_PER_PASS)
        .with_for_update(skip_locked=True)
    )
    for narrative in due.all():
        if await nar.elect_narrative_medoids(session, actor, narrative, now=now):
            changes += 1

    changes += await _split_flags(session, settings)

    purged = await session.execute(
        delete(NarrativeStat).where(
            NarrativeStat.window_end < now - timedelta(days=STATS_RETENTION_DAYS)
        )
    )
    purged_rarity = await session.execute(
        delete(EntityRarity).where(
            EntityRarity.day < now.date() - timedelta(days=RARITY_RETENTION_DAYS)
        )
    )
    changes += int(purged.rowcount or 0) + int(purged_rarity.rowcount or 0)  # type: ignore[attr-defined]
    return changes


async def _split_flags(session: AsyncSession, settings: Settings) -> int:
    """``split_suggested`` above ``fc_narrative_split_groups`` groups, or when one group
    bridges two otherwise disconnected subsets (small narratives only)."""
    counts = await session.execute(
        select(NarrativeGroup.narrative_id, func.count())
        .join(Narrative, Narrative.id == NarrativeGroup.narrative_id)
        .where(
            NarrativeGroup.removed_at.is_(None),
            Narrative.state != NarrativeState.CLOSED.value,
            Narrative.merged_into_id.is_(None),
        )
        .group_by(NarrativeGroup.narrative_id)
    )
    size = {int(n): int(c) for n, c in counts.all()}
    if not size:
        return 0
    flagged = {n: c > settings.fc_narrative_split_groups for n, c in size.items()}
    recent = await session.scalars(
        select(Narrative.id)
        .where(Narrative.id.in_(list(size)))
        .order_by(Narrative.last_activity_at.desc(), Narrative.id)
    )
    checked = 0
    for nid in recent.all():
        nid = int(nid)
        if flagged[nid] or not 5 <= size[nid] <= MAX_SPLIT_GRAPH_GROUPS:
            continue
        if checked >= MAX_SPLIT_CHECKS:
            break
        checked += 1
        group_ids = await nar.active_group_ids(session, nid)
        edges: set[tuple[int, int]] = set()
        co = await session.execute(
            text(
                "SELECT DISTINCT m1.group_id, m2.group_id FROM claim_group_members m1 "
                "JOIN content_claims c1 ON c1.claim_id = m1.claim_id "
                "JOIN content_claims c2 ON c2.content_id = c1.content_id "
                "JOIN claim_group_members m2 ON m2.claim_id = c2.claim_id "
                "WHERE m1.group_id = ANY(:g) AND m2.group_id = ANY(:g) "
                "AND m1.group_id < m2.group_id AND m1.removed_at IS NULL AND m2.removed_at IS NULL"
            ),
            {"g": group_ids},
        )
        edges.update((int(a), int(b)) for a, b in co.all())
        sem = await session.execute(
            text(
                "SELECT a.id, b.id FROM claim_groups a JOIN claim_groups b ON a.id < b.id "
                "WHERE a.id = ANY(:g) AND b.id = ANY(:g) "
                "AND a.representative_embedding IS NOT NULL "
                "AND b.representative_embedding IS NOT NULL "
                "AND 1 - (a.representative_embedding <=> b.representative_embedding) >= :thr"
            ),
            {"g": group_ids, "thr": settings.fc_narrative_semantic_threshold},
        )
        edges.update((int(a), int(b)) for a, b in sem.all())
        flagged[nid] = find_bridge(group_ids, edges)
    current = {
        int(n): bool(f)
        for n, f in (
            await session.execute(
                select(Narrative.id, Narrative.split_suggested).where(Narrative.id.in_(list(size)))
            )
        ).all()
    }
    changed = 0
    for nid, flag in flagged.items():
        if current.get(nid) != flag:
            await session.execute(
                text(
                    "UPDATE narratives SET split_suggested = :f, updated_at = now() WHERE id = :i"
                ),
                {"f": flag, "i": nid},
            )
            changed += 1
    return changed


# ------------------------------------------------------------------ surge sampling


_SIM_SQL = """
SELECT ce.content_id, n.id, max(1 - (ce.embedding <=> g.representative_embedding))
FROM (SELECT DISTINCT ON (content_id) content_id, embedding FROM content_embeddings
      WHERE content_id = ANY(:ids) ORDER BY content_id, created_at DESC) ce
CROSS JOIN narratives n
JOIN claim_groups g ON g.id = ANY(n.medoid_group_ids)
WHERE n.id = ANY(:nids) AND g.representative_embedding IS NOT NULL
GROUP BY ce.content_id, n.id
"""

_RECENT_CLOSE_SQL = """
SELECT n.id, count(DISTINCT x.content_id)
FROM content_extractions x
JOIN LATERAL (SELECT embedding FROM content_embeddings WHERE content_id = x.content_id
              ORDER BY created_at DESC LIMIT 1) ce ON true
CROSS JOIN narratives n
JOIN claim_groups g ON g.id = ANY(n.medoid_group_ids)
WHERE x.created_at > :since AND n.id = ANY(:nids) AND x.status = ANY(:statuses)
  AND g.representative_embedding IS NOT NULL
  AND 1 - (ce.embedding <=> g.representative_embedding) >= :thr
GROUP BY n.id
"""

_NOVELTY_UPSERT = """
INSERT INTO narrative_novelty
  (narrative_id, fingerprint, kind, posts, accounts, account_ids, best_content_id,
   best_engagement, first_seen_at, last_seen_at)
VALUES (:nid, :fp, :kind, :posts, cardinality(CAST(:accounts AS bigint[])),
        CAST(:accounts AS bigint[]), :best, :eng, now(), now())
ON CONFLICT (narrative_id, fingerprint) DO UPDATE SET
  posts = narrative_novelty.posts + EXCLUDED.posts,
  account_ids = (SELECT COALESCE(array_agg(a), '{}') FROM
                 (SELECT DISTINCT a FROM unnest(
                    narrative_novelty.account_ids || EXCLUDED.account_ids) a
                  ORDER BY a LIMIT 10) s),
  accounts = cardinality((SELECT COALESCE(array_agg(a), '{}') FROM
                 (SELECT DISTINCT a FROM unnest(
                    narrative_novelty.account_ids || EXCLUDED.account_ids) a
                  ORDER BY a LIMIT 10) s)),
  best_content_id = CASE WHEN EXCLUDED.best_engagement > narrative_novelty.best_engagement
                         THEN EXCLUDED.best_content_id ELSE narrative_novelty.best_content_id END,
  best_engagement = GREATEST(EXCLUDED.best_engagement, narrative_novelty.best_engagement),
  last_seen_at = now()
"""


async def _recent_close(
    session: AsyncSession,
    nids: Sequence[int],
    since: datetime,
    statuses: Sequence[str],
) -> dict[int, int]:
    rows = await session.execute(
        text(_RECENT_CLOSE_SQL),
        {"nids": list(nids), "since": since, "statuses": list(statuses), "thr": SAMPLING_COSINE},
    )
    return {int(n): int(c) for n, c in rows.all()}


@dataclass(slots=True)
class _Seen:
    kind: str
    posts: int = 0
    accounts: set[int] = field(default_factory=set)
    best_content: int | None = None
    best_engagement: int = -1


async def filter_for_surges(
    session: AsyncSession,
    settings: Settings,
    content_ids: Sequence[int],
    *,
    rng: random.Random | None = None,
) -> tuple[list[int], dict[int, int]]:
    """Called by the batch composer before extraction: returns the contents to extract
    and ``{content_id: narrative_id}`` of the contents only counted (surge sampling);
    forced novelties are part of the first list.

    Only contents whose E1 vector is close (cosine >= 0.85) to a medoid of a narrative at
    surge/alert are subject to sampling, and only beyond ``fc_sampling_per_minute``."""
    ids = list(dict.fromkeys(content_ids))
    if not ids:
        return [], {}
    rng = rng or random.Random()  # noqa: S311 (sampling, not security)
    now = datetime.now(UTC)
    surging = [
        int(n)
        for n in (
            await session.scalars(
                select(Narrative.id)
                .where(
                    Narrative.state.in_(WAVE_STATES),
                    Narrative.merged_into_id.is_(None),
                    func.cardinality(Narrative.medoid_group_ids) > 0,
                )
                .order_by(Narrative.surge_started_at.desc().nulls_last(), Narrative.id)
                .limit(MAX_SURGING_NARRATIVES)
            )
        ).all()
    ]
    if not surging:
        return list(content_ids), {}

    sims = await session.execute(text(_SIM_SQL), {"ids": ids, "nids": surging})
    best: dict[int, tuple[float, int]] = {}
    for content_id, nid, sim in sims.all():
        if sim is not None and float(sim) >= SAMPLING_COSINE:
            key = int(content_id)
            if key not in best or (float(sim), -int(nid)) > (best[key][0], -best[key][1]):
                best[key] = (float(sim), int(nid))
    if not best:
        return list(content_ids), {}
    by_narrative: dict[int, list[int]] = {}
    for content_id in ids:
        if content_id in best:
            by_narrative.setdefault(best[content_id][1], []).append(content_id)

    nids = sorted(by_narrative)
    per_minute = await _recent_close(
        session,
        nids,
        now - timedelta(minutes=1),
        ("extracted", "no_claim", "sampled_out"),
    )
    info = {
        int(r[0]): r
        for r in (
            await session.execute(
                select(
                    ContentItem.id,
                    ContentItem.platform,
                    ContentItem.created_at_platform,
                    ContentItem.first_seen_at,
                    ContentItem.text,
                    ContentItem.metrics,
                    ContentItem.author_account_id,
                ).where(ContentItem.id.in_(list(best)))
            )
        ).all()
    }
    sampled: dict[int, int] = {}
    kept_count: dict[int, int] = {}
    for nid in nids:
        members = by_narrative[nid]
        rate = per_minute.get(nid, 0) + len(members)
        share = sampling_rate(rate, settings.fc_sampling_per_minute)
        strata = []
        for content_id in members:
            row = info.get(content_id)
            when = (row[2] or row[3]) if row is not None else now
            strata.append(
                (content_id, (row[1] if row is not None else "x", when.strftime("%Y%m%d%H")))
            )
        kept, dropped = stratified_sample(strata, share, rng)
        kept_count[nid] = len(kept)
        for content_id in dropped:
            sampled[content_id] = nid

    forced = await _novelty(session, settings, sampled, kept_count, info, now)
    for content_id in forced:
        sampled.pop(content_id, None)
    to_extract = [c for c in content_ids if c not in sampled]
    to_extract += [c for c in forced if c not in set(content_ids)]
    if sampled or forced:
        log.info("surge.sampled", sampled_out=len(sampled), forced=len(forced))
    return to_extract, sampled


async def _novelty(
    session: AsyncSession,
    settings: Settings,
    sampled: Mapping[int, int],
    kept_count: Mapping[int, int],
    info: Mapping[int, Any],
    now: datetime,
) -> list[int]:
    """Count the novelty fingerprints of the sampled-out contents and return the contents
    forced into extraction (bounded by ``fc_novelty_budget_share`` per narrative-hour)."""
    if not sampled:
        return []
    media: dict[int, list[str]] = {}
    for content_id, digest in (
        await session.execute(
            text(
                "SELECT cm.content_id, encode(ma.sha256, 'hex') FROM content_media cm "
                "JOIN media_assets ma ON ma.id = cm.media_id "
                "WHERE cm.content_id = ANY(:ids) AND ma.sha256 IS NOT NULL"
            ),
            {"ids": list(sampled)},
        )
    ).all():
        media.setdefault(int(content_id), []).append(str(digest))

    seen: dict[tuple[int, str], _Seen] = {}
    engagement: dict[int, int] = {}
    for content_id, nid in sampled.items():
        row = info.get(content_id)
        if row is None:
            continue
        eng = engagement_of(row[5])
        engagement[content_id] = eng
        for kind, fingerprint in novelty_fingerprints(
            str(row[4]), media_hashes=media.get(content_id, ())
        ):
            entry = seen.setdefault((nid, fingerprint), _Seen(kind=kind))
            entry.posts += 1
            if row[6] is not None:
                entry.accounts.add(int(row[6]))
            if eng > entry.best_engagement:
                entry.best_engagement, entry.best_content = eng, content_id
    if seen:
        await session.execute(
            text(_NOVELTY_UPSERT),
            [
                {
                    "nid": nid,
                    "fp": fingerprint,
                    "kind": entry.kind,
                    "posts": entry.posts,
                    "accounts": sorted(entry.accounts)[:10],
                    "best": entry.best_content,
                    "eng": max(entry.best_engagement, 0),
                }
                for (nid, fingerprint), entry in seen.items()
            ],
        )

    forced: list[int] = []
    for nid in sorted(set(sampled.values())):
        fingerprints = [fp for (n, fp) in seen if n == nid]
        taking = []
        if fingerprints:
            rows = await session.execute(
                text(
                    "SELECT fingerprint, best_content_id, best_engagement, posts, accounts "
                    "FROM narrative_novelty WHERE narrative_id = :n AND forced_at IS NULL "
                    "AND fingerprint = ANY(:fps) AND best_content_id IS NOT NULL"
                ),
                {"n": nid, "fps": fingerprints},
            )
            taking = [
                (str(fp), int(best_id), int(eng))
                for fp, best_id, eng, posts, accounts in rows.all()
                if novelty_takes(
                    int(posts),
                    int(accounts),
                    min_posts=settings.fc_novelty_posts,
                    min_accounts=settings.fc_novelty_accounts,
                )
            ]
        engagements = [
            engagement_of(m)
            for (m,) in (
                await session.execute(
                    text(
                        f"WITH nc AS ({nar.NARRATIVE_CONTENTS}) "  # noqa: S608
                        "SELECT ci.metrics FROM nc JOIN content_items ci ON ci.id = nc.content_id "
                        "ORDER BY ci.id DESC LIMIT 500"
                    ),
                    {"nids": [nid]},
                )
            ).all()
        ]
        p90 = (
            percentile(engagements, 0.9)
            if len(engagements) >= NOVELTY_PERCENTILE_MIN_SAMPLES
            else math.inf
        )
        candidates: dict[int, int] = {best_id: eng for _, best_id, eng in taking}
        for content_id, content_nid in sampled.items():
            if content_nid == nid and engagement.get(content_id, 0) > p90:
                candidates[content_id] = engagement[content_id]
        if not candidates:
            continue
        extracted_hour = (
            await _recent_close(session, [nid], now - timedelta(hours=1), ("extracted", "no_claim"))
        ).get(nid, 0)
        forced_hour = int(
            await session.scalar(
                text(
                    "SELECT count(DISTINCT best_content_id) FROM narrative_novelty "
                    "WHERE narrative_id = :n AND forced_at > :since"
                ),
                {"n": nid, "since": now - timedelta(hours=1)},
            )
            or 0
        )
        budget = (
            math.floor(settings.fc_novelty_budget_share * (extracted_hour + kept_count.get(nid, 0)))
            - forced_hour
        )
        if budget <= 0:
            continue
        chosen = sorted(candidates, key=lambda c: (-candidates[c], c))[:budget]
        already = {
            int(r[0])
            for r in (
                await session.execute(
                    text(
                        "SELECT content_id FROM content_extractions "
                        "WHERE content_id = ANY(:ids) AND status <> 'sampled_out'"
                    ),
                    {"ids": chosen},
                )
            ).all()
        }
        chosen = [c for c in chosen if c not in already]
        if not chosen:
            continue
        await session.execute(
            text(
                "UPDATE narrative_novelty SET forced_at = :now WHERE narrative_id = :n "
                "AND best_content_id = ANY(:ids) AND fingerprint = ANY(:fps)"
            ),
            {"now": now, "n": nid, "ids": chosen, "fps": [fp for fp, _, _ in taking]},
        )
        forced.extend(chosen)
    return forced
