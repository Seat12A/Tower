"""Claims of a content: recording an extraction (E2) and promoting a group to N4.

Both write through the use case rules: permission check, domain rules, events published
in the same transaction as the rows (see docs/factcheck/pipeline.md).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain.factcheck.matching import MatchDecision
from tower.core.domain.factcheck.opinion import initial_depth
from tower.core.domain.factcheck.profiles import Profile
from tower.core.domain.factcheck.reuse import ReuseContext, can_reuse
from tower.core.domain.factcheck.signatures import NormalizedSignature, compare_signatures
from tower.core.domain.factcheck.values import (
    UNVERIFIABLE_CLAIM_TYPES,
    ClaimGroupState,
    ClaimType,
    ExtractionStatus,
    GroupEventKind,
    MatchOutcome,
    ReuseRule,
    VerdictStatus,
)
from tower.core.domain.factcheck.vectors import cosine
from tower.core.domain.security import Actor, Permission
from tower.core.events.outbox import publish
from tower.core.factcheck import names
from tower.core.factcheck.candidates import claim_signature, nearest_groups, post_dates
from tower.core.factcheck.references import active_band
from tower.core.factcheck.schemas import ExtractedClaim
from tower.core.logging import get_logger
from tower.core.models.factcheck import (
    LEVELS,
    Claim,
    ClaimGroup,
    ClaimGroupMember,
    ContentClaim,
    Investigation,
    Verdict,
)
from tower.core.models.factcheck_v1 import ClaimGroupEvent, ReferenceFact
from tower.core.models.factcheck_v2 import ContentExtraction
from tower.core.models.ingestion import ContentItem
from tower.core.usecases.reuse import inherit_band_verdict

__all__ = [
    "ExtractionRecord",
    "journal_event",
    "mark_extraction",
    "promote_to_n4",
    "raise_group_level",
    "record_extraction",
    "request_investigation",
]

log = get_logger(__name__)

FRAMING = "framing"
FAILED = "failed"
_RUNNING_EXCLUDED = ("completed", "assembled", "failed")
_MAX_MERGE_HOPS = 5


@dataclass(slots=True)
class ExtractionRecord:
    claim_ids: list[int] = field(default_factory=list)
    group_ids: list[int] = field(default_factory=list)
    investigation_ids: list[int] = field(default_factory=list)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _decimal(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(repr(value))


def _impressions(content: ContentItem) -> int:
    value = (content.metrics or {}).get("impressions")
    return int(value) if isinstance(value, int | float) and value > 0 else 0


async def request_investigation(
    session: AsyncSession,
    actor: Actor,
    *,
    group: ClaimGroup,
    content: ContentItem,
    level: str,
    requested_by: str,
    flags: Mapping[str, Any] | None = None,
) -> Investigation:
    """New investigation of ``group`` (stage framing) and its ``investigation.requested``."""
    depth = initial_depth(level)
    investigation = Investigation(
        public_id=uuid.uuid4(),
        claim_group_id=group.id,
        content_id=content.id,
        level=level,
        depth=depth.value,
        stage=FRAMING,
        requested_by=requested_by,
        flags=dict(flags or {}),
    )
    session.add(investigation)
    await session.flush()
    await publish(
        session,
        names.INVESTIGATION_REQUESTED,
        {
            "investigation_id": str(investigation.public_id),
            "group_id": str(group.public_id),
            "level": level,
            "depth": depth.value,
            "content_id": str(content.id),
        },
        aggregate_type="investigation",
        aggregate_id=str(investigation.public_id),
        actor=actor.label,
    )
    return investigation


async def journal_event(
    session: AsyncSession,
    actor: Actor,
    group: ClaimGroup,
    kind: GroupEventKind,
    *,
    other_group_id: int | None = None,
    claim_ids: Sequence[int] = (),
    from_level: str | None = None,
    to_level: str | None = None,
    reason: str | None = None,
    data: Mapping[str, Any] | None = None,
) -> None:
    """One row of the group journal (``claim_group_events``)."""
    session.add(
        ClaimGroupEvent(
            group_id=group.id,
            kind=kind.value,
            other_group_id=other_group_id,
            claim_ids=list(claim_ids),
            from_level=from_level,
            to_level=to_level,
            actor=actor.label,
            reason=reason,
            data=dict(data or {}),
        )
    )
    await session.flush()


async def raise_group_level(
    session: AsyncSession, actor: Actor, group: ClaimGroup, level: str, *, reason: str
) -> bool:
    """Raise ``group`` to ``level`` when higher (never lowers): journal and
    ``claim_group.level_changed``. Returns True when the level changed."""
    if LEVELS.index(level) <= LEVELS.index(group.level):
        return False
    previous = group.level
    group.level = level
    group.updated_at = func.now()
    await journal_event(
        session,
        actor,
        group,
        GroupEventKind.LEVEL_CHANGED,
        from_level=previous,
        to_level=level,
        reason=reason,
    )
    await publish(
        session,
        names.CLAIM_GROUP_LEVEL_CHANGED,
        {
            "group_id": str(group.public_id),
            "from_level": previous,
            "to_level": level,
            "reason": reason,
        },
        aggregate_type="claim_group",
        aggregate_id=str(group.public_id),
        actor=actor.label,
    )
    return True


def _informative(sig: NormalizedSignature) -> bool:
    """A signature carrying a value: enough to say two claims of one post are the same."""
    return sig.value_low is not None or sig.value_high is not None


async def _live_group(session: AsyncSession, group_id: int) -> ClaimGroup | None:
    """Lock ``group_id`` and follow ``merged_into_id`` (at most 5 hops) to the live group."""
    current = group_id
    for _ in range(_MAX_MERGE_HOPS + 1):
        group = await session.scalar(
            select(ClaimGroup)
            .where(ClaimGroup.id == current)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if group is None:
            return None
        if group.state != ClaimGroupState.MERGED.value:
            return group
        if group.merged_into_id is None:
            return None
        current = group.merged_into_id
    return None


async def _recheck_concurrent(
    session: AsyncSession,
    *,
    claim: Claim,
    normalized: NormalizedSignature,
    decision: MatchDecision,
    profile: Profile | None,
    current_period_days: int,
) -> tuple[ClaimGroup | None, dict[str, Any]]:
    """Before creating a group: serialize the creations of the same signature family
    (transaction advisory lock) and look again, in the database only, for a group a
    concurrent extraction created after E3 ran. Only the no-arbiter rule applies here
    (identical signature, same language, cosine of original vectors >= threshold); any
    other case stays a new group (the arbiter cannot be called inside a transaction)."""
    key = "|".join(
        [
            "fc-group",
            normalized.claim_type,
            normalized.subject_key or "",
            normalized.territory or "",
        ]
    )
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})
    pivot = decision.embedding_pivot or decision.embedding
    if not pivot or not decision.embedding:
        return None, {}
    threshold = profile.match_same_threshold if profile is not None else 0.92
    for row in await nearest_groups(session, list(pivot), limit=5):
        if row.claim_lang != claim.lang_original or row.original_embedding is None:
            continue
        comparison = compare_signatures(
            normalized, row.signature, current_period_days=current_period_days
        )
        if not comparison.identical:
            continue
        similarity = cosine(decision.embedding, row.original_embedding)
        if similarity < threshold:
            continue
        group = await _live_group(session, row.group_id)
        if group is not None:
            log.info("factcheck.join_on_recheck", group=group.id, claim=claim.id)
            return group, {"rule": "identical_signature:recheck", "similarity": similarity}
    return None, {}


async def _identical_to_representative(
    session: AsyncSession,
    group: ClaimGroup,
    claim: Claim,
    post_day: date | None,
    current_period_days: int,
) -> bool:
    """Re-check under the lock (a concurrent merge may have changed the representative)."""
    representative = await session.get(Claim, group.representative_claim_id)
    if representative is None:
        return False
    day = (await post_dates(session, [representative.id])).get(representative.id)
    comparison = compare_signatures(
        claim_signature(claim, post_day),
        claim_signature(representative, day),
        current_period_days=current_period_days,
    )
    return comparison.identical


async def _has_open_work(session: AsyncSession, group: ClaimGroup) -> bool:
    """A running investigation, or a draft verdict waiting for review."""
    running = await session.scalar(
        select(Investigation.id)
        .where(
            Investigation.claim_group_id == group.id,
            Investigation.stage.not_in(_RUNNING_EXCLUDED),
        )
        .limit(1)
    )
    if running is not None:
        return True
    draft = await session.scalar(
        select(Verdict.id)
        .where(
            Verdict.claim_group_id == group.id,
            Verdict.status == VerdictStatus.DRAFT.value,
        )
        .limit(1)
    )
    return draft is not None


def _direct_reuse_allowed(profile: Profile | None, level: str) -> bool:
    return profile.level(level).direct_reuse if profile is not None else level != "N4"


async def _after_join(
    session: AsyncSession,
    actor: Actor,
    *,
    group: ClaimGroup,
    claim: Claim,
    content: ContentItem,
    level: str,
    requested_by: str,
    profile: Profile | None,
    flags: Mapping[str, Any],
    language_not_processed: bool,
    record: ExtractionRecord,
    investigate: bool = True,
) -> None:
    """What happens to a claim that joined ``group`` (every member is covered by the
    group verdict: joining is direct reuse)."""
    if level == "N4":
        investigation = await promote_to_n4(
            session,
            actor,
            group=group,
            content=content,
            requested_by=requested_by,
            language_not_processed=language_not_processed,
        )
        if investigation is not None:
            record.investigation_ids.append(investigation.id)
        return
    verdict = (
        await session.get(Verdict, group.active_verdict_id)
        if group.active_verdict_id is not None
        else None
    )
    now = datetime.now(UTC)
    if verdict is not None and can_reuse(
        ReuseContext(
            level=level,
            direct_reuse_allowed=_direct_reuse_allowed(profile, level),
            verdict_expires_at=verdict.expires_at,
            verdict_status=verdict.status,
            note_regime=None,
            note_expires_at=None,
            now=now,
        ),
        ReuseRule.IDENTICAL_SIGNATURE,
    ):
        await publish(
            session,
            names.VERDICT_REUSED,
            {
                "verdict_id": str(verdict.public_id),
                "group_id": str(group.public_id),
                "claim_id": str(claim.public_id),
                "content_id": str(content.id),
                "rule": ReuseRule.IDENTICAL_SIGNATURE.value,
                "origin_verdict_id": str(verdict.public_id),
            },
            aggregate_type="claim_group",
            aggregate_id=str(group.public_id),
            actor=actor.label,
        )
        return
    if not investigate:
        return
    if await _has_open_work(session, group):
        return
    if ClaimType(claim.claim_type) in UNVERIFIABLE_CLAIM_TYPES:
        return
    investigation = await request_investigation(
        session,
        actor,
        group=group,
        content=content,
        level=level,
        requested_by=requested_by,
        flags=flags,
    )
    record.investigation_ids.append(investigation.id)


async def _try_band_inheritance(
    session: AsyncSession,
    actor: Actor,
    *,
    group: ClaimGroup,
    claim: Claim,
    content: ContentItem,
    level: str,
    decision: MatchDecision,
    profile: Profile | None,
) -> bool:
    """Inherit the verdict of a validated band of a validated fact (rule ``band``)."""
    if (
        level == "N4"
        or decision.reference_fact_id is None
        or decision.band is None
        or decision.band_side is None
        or claim.claim_type != ClaimType.STATISTIC.value
    ):
        return False
    fact = await session.get(ReferenceFact, decision.reference_fact_id)
    if fact is None or fact.status != "validated":
        return False
    band = await active_band(session, fact.id, decision.band, decision.band_side)
    if band is None or band.source_verdict_id is None:
        return False
    source = await session.get(Verdict, band.source_verdict_id)
    if source is None:
        return False
    now = datetime.now(UTC)
    if fact.next_publication is not None and fact.next_publication <= now.date():
        return False
    allowed = can_reuse(
        ReuseContext(
            level=level,
            direct_reuse_allowed=_direct_reuse_allowed(profile, level),
            verdict_expires_at=source.expires_at,
            verdict_status=source.status,
            note_regime=None,
            note_expires_at=None,
            now=now,
        ),
        ReuseRule.BAND,
    )
    if not allowed:
        return False
    await inherit_band_verdict(
        session,
        actor,
        group=group,
        band=band,
        fact=fact,
        claim=claim,
        content_id=content.id,
        now=now,
    )
    return True


async def _group_public_id(session: AsyncSession, group_id: int | None) -> str | None:
    if group_id is None:
        return None
    value = await session.scalar(select(ClaimGroup.public_id).where(ClaimGroup.id == group_id))
    return str(value) if value is not None else None


async def record_extraction(
    session: AsyncSession,
    actor: Actor,
    *,
    content: ContentItem,
    claims: Sequence[ExtractedClaim],
    level: str,
    requested_by: str,
    prompt_version: str,
    thread_context: str | None,
    absence_reason: str | None = None,
    language_not_processed: bool = False,
    matches: Sequence[MatchDecision] | None = None,
    post_date: date | None = None,
    profile: Profile | None = None,
    current_period_days: int = 31,
    investigate: bool = True,
    extraction_status: ExtractionStatus | None = ExtractionStatus.EXTRACTED,
    batch_id: int | None = None,
    position: int | None = None,
) -> ExtractionRecord:
    """Store the claims of ``content``; publish ``claim.extracted`` and
    ``investigation.requested``.

    Without ``matches`` (lot 3 behaviour) every claim gets its own group and every
    verifiable claim an investigation. With ``matches`` (E3 decisions, same length and
    order as ``claims``) a claim joins an existing group (direct reuse of its verdict, or
    nothing when work is under way), or creates a group that may inherit a verdict
    through a validated value band.

    ``investigate=False`` (N1 mass flow): new groups are ``dormant`` and no investigation is
    requested (the scheduler decides); joins and verdict reuse are unchanged.

    ``extraction_status`` (default ``extracted``; ``None`` writes nothing) upserts the
    ``content_extractions`` row of the content: ``no_claim`` when no claim was kept.
    ``batch_id`` and ``position`` locate a batched post.

    No claim (absence reason only) writes no claim."""
    actor.require(Permission.FACTCHECK_REQUEST)
    record = ExtractionRecord()
    if not claims:
        log.info("factcheck.no_claim", content=content.id, reason=absence_reason)
        if extraction_status is not None:
            await mark_extraction(
                session, content.id, level, ExtractionStatus.NO_CLAIM, batch_id, position
            )
        return record
    if matches is not None and len(matches) != len(claims):
        raise ValueError("matches must have one decision per claim")
    if post_date is None and content.created_at_platform is not None:
        post_date = content.created_at_platform.date()

    base_flags: dict[str, Any] = {"language_not_processed": True} if language_not_processed else {}
    new_state = ClaimGroupState.QUEUED if investigate else ClaimGroupState.DORMANT
    claim_rows: list[Claim] = []
    group_rows: list[ClaimGroup] = []
    created_in_call: list[tuple[NormalizedSignature, Claim, ClaimGroup]] = []
    for claim_position, extracted in enumerate(claims):
        decision = matches[claim_position] if matches is not None else None
        sig = extracted.signature
        claim = Claim(
            public_id=uuid.uuid4(),
            lang_original=content.lang,
            text_original=extracted.text_original,
            text_fr=extracted.text_fr,
            claim_type=extracted.claim_type.value,
            subject=sig.subject,
            subject_qid=sig.subject_qid,
            predicate=sig.predicate,
            value_low=_decimal(sig.value_low),
            value_high=_decimal(sig.value_high),
            unit=sig.unit,
            unit_scale=sig.unit_scale,
            currency=sig.currency,
            value_date=_parse_date(sig.value_date),
            period_start=_parse_date(sig.period_start),
            period_end=_parse_date(sig.period_end),
            place=sig.place,
            polarity=sig.polarity.value,
            invoked_source=sig.invoked_source,
            definition_variant=sig.definition_variant,
            comparison_base=sig.comparison_base,
            jurisdictions=list(extracted.jurisdictions),
            primary_jurisdiction=extracted.primary_jurisdiction,
            entities=[e.model_dump(mode="json") for e in extracted.entities],
            theme=extracted.theme,
            verifiability=extracted.verifiability,
            context_hints=[h.value for h in extracted.context_hints],
            source_modality=extracted.source_modality,
            thread_context=thread_context,
            prompt_version=prompt_version,
            embedding=list(decision.embedding) if decision and decision.embedding else None,
            embedding_pivot=(
                list(decision.embedding_pivot) if decision and decision.embedding_pivot else None
            ),
            embedding_model=decision.embedding_model if decision else None,
        )
        session.add(claim)
        await session.flush()
        session.add(ContentClaim(content_id=content.id, claim_id=claim.id, position=claim_position))
        record.claim_ids.append(claim.id)
        claim_rows.append(claim)

        if decision is None:
            group = ClaimGroup(
                public_id=uuid.uuid4(),
                representative_claim_id=claim.id,
                level=level,
                state=new_state.value,
                reach=_impressions(content),
            )
            session.add(group)
            await session.flush()
            session.add(ClaimGroupMember(group_id=group.id, claim_id=claim.id))
            group_rows.append(group)
            record.group_ids.append(group.id)
            if investigate and ClaimType(claim.claim_type) not in UNVERIFIABLE_CLAIM_TYPES:
                investigation = await request_investigation(
                    session,
                    actor,
                    group=group,
                    content=content,
                    level=level,
                    requested_by=requested_by,
                    flags=base_flags,
                )
                record.investigation_ids.append(investigation.id)
            continue

        # ---- E3 decision: join a live group whose signature still matches, else new.
        normalized = claim_signature(claim, post_date)
        joined: ClaimGroup | None = None
        join_data: dict[str, Any] = {}
        if decision.outcome is MatchOutcome.JOIN and decision.group_id is not None:
            candidate = await _live_group(session, decision.group_id)
            if candidate is not None and await _identical_to_representative(
                session, candidate, claim, post_date, current_period_days
            ):
                joined = candidate
                join_data = {
                    "rule": decision.rule,
                    "similarity": decision.similarity,
                    "arbiter_reason": decision.arbiter_reason,
                }
            else:
                log.info("factcheck.join_rejected_on_recheck", group=decision.group_id)
        if joined is None:
            for earlier_sig, earlier_claim, earlier_group in created_in_call:
                comparison = compare_signatures(
                    normalized, earlier_sig, current_period_days=current_period_days
                )
                if comparison.identical and (
                    _informative(normalized)
                    or earlier_claim.text_original.casefold() == claim.text_original.casefold()
                ):
                    joined = earlier_group
                    join_data = {"rule": "same_post:identical_signature"}
                    break

        if joined is None:
            joined, join_data = await _recheck_concurrent(
                session,
                claim=claim,
                normalized=normalized,
                decision=decision,
                profile=profile,
                current_period_days=current_period_days,
            )

        if joined is not None:
            session.add(ClaimGroupMember(group_id=joined.id, claim_id=claim.id))
            joined.reach = joined.reach + _impressions(content)
            joined.updated_at = func.now()
            await journal_event(
                session,
                actor,
                joined,
                GroupEventKind.JOINED,
                claim_ids=[claim.id],
                data=join_data,
            )
            await raise_group_level(session, actor, joined, level, reason="content")
            await _after_join(
                session,
                actor,
                group=joined,
                claim=claim,
                content=content,
                level=level,
                requested_by=requested_by,
                profile=profile,
                flags=base_flags,
                language_not_processed=language_not_processed,
                record=record,
                investigate=investigate,
            )
            group_rows.append(joined)
            record.group_ids.append(joined.id)
            continue

        group = ClaimGroup(
            public_id=uuid.uuid4(),
            representative_claim_id=claim.id,
            level=level,
            state=new_state.value,
            reach=_impressions(content),
            representative_embedding=(
                list(decision.embedding_pivot or decision.embedding or ()) or None
            ),
            reference_fact_id=decision.reference_fact_id,
            band=decision.band.value if decision.band else None,
            band_side=decision.band_side.value if decision.band_side else None,
        )
        session.add(group)
        await session.flush()
        session.add(ClaimGroupMember(group_id=group.id, claim_id=claim.id))
        fact_public = None
        if decision.reference_fact_id is not None:
            fact_public = await session.scalar(
                select(ReferenceFact.public_id).where(
                    ReferenceFact.id == decision.reference_fact_id
                )
            )
        await journal_event(
            session,
            actor,
            group,
            GroupEventKind.CREATED,
            other_group_id=decision.related_group_id,
            claim_ids=[claim.id],
            to_level=level,
            data={
                "rule": decision.rule,
                "similarity": decision.similarity,
                "arbiter_reason": decision.arbiter_reason,
                "related_group_id": decision.related_group_id,
            },
        )
        if decision.reference_fact_id is not None:
            await journal_event(
                session,
                actor,
                group,
                GroupEventKind.FACT_LINKED,
                data={
                    "reference_fact_id": str(fact_public),
                    "band": group.band,
                    "band_side": group.band_side,
                },
            )
        await publish(
            session,
            names.CLAIM_GROUP_CREATED,
            {
                "group_id": str(group.public_id),
                "claim_id": str(claim.public_id),
                "level": level,
                "content_id": str(content.id),
                "rule": decision.rule,
                "related_group_id": await _group_public_id(session, decision.related_group_id),
                "reference_fact_id": str(fact_public) if fact_public is not None else None,
                "band": group.band,
                "band_side": group.band_side,
            },
            aggregate_type="claim_group",
            aggregate_id=str(group.public_id),
            actor=actor.label,
        )
        created_in_call.append((normalized, claim, group))
        group_rows.append(group)
        record.group_ids.append(group.id)
        if await _try_band_inheritance(
            session,
            actor,
            group=group,
            claim=claim,
            content=content,
            level=level,
            decision=decision,
            profile=profile,
        ):
            continue
        if investigate and ClaimType(claim.claim_type) not in UNVERIFIABLE_CLAIM_TYPES:
            flags = dict(base_flags)
            if decision.outdated:
                flags["outdated"] = True
            if fact_public is not None:
                flags["reference_fact_id"] = str(fact_public)
            if decision.external_factchecks:
                flags["external_factchecks"] = [dict(e) for e in decision.external_factchecks]
            investigation = await request_investigation(
                session,
                actor,
                group=group,
                content=content,
                level=level,
                requested_by=requested_by,
                flags=flags,
            )
            record.investigation_ids.append(investigation.id)

    await publish(
        session,
        names.CLAIM_EXTRACTED,
        {
            "content_id": str(content.id),
            "content_public_id": str(content.public_id),
            "platform": content.platform,
            "external_id": content.external_id,
            "level": level,
            "claim_ids": [str(c.public_id) for c in claim_rows],
            "group_ids": [str(g.public_id) for g in group_rows],
        },
        aggregate_type="content",
        aggregate_id=str(content.public_id),
        actor=actor.label,
    )
    if extraction_status is not None:
        await mark_extraction(
            session, content.id, level, ExtractionStatus.EXTRACTED, batch_id, position
        )
    return record


async def mark_extraction(
    session: AsyncSession,
    content_id: int,
    level: str,
    status: ExtractionStatus,
    batch_id: int | None,
    position: int | None,
) -> None:
    """Upsert the ``content_extractions`` row (one per content)."""
    values = {
        "content_id": content_id,
        "level": level,
        "status": status.value,
        "batch_id": batch_id,
        "position": position,
    }
    await session.execute(
        pg_insert(ContentExtraction)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[ContentExtraction.content_id],
            set_={k: v for k, v in values.items() if k != "content_id"},
        )
    )


async def promote_to_n4(
    session: AsyncSession,
    actor: Actor,
    *,
    group: ClaimGroup,
    content: ContentItem,
    requested_by: str,
    language_not_processed: bool = False,
) -> Investigation | None:
    """Group → level N4. Starts a reinforced investigation unless the group already has
    an N4 one that did not fail. Returns the new investigation, if any."""
    actor.require(Permission.FACTCHECK_REQUEST)
    group.level = "N4"
    group.updated_at = func.now()
    claim_type = await session.scalar(
        select(Claim.claim_type).where(Claim.id == group.representative_claim_id)
    )
    if claim_type is not None and ClaimType(claim_type) in UNVERIFIABLE_CLAIM_TYPES:
        return None
    existing = await session.scalar(
        select(Investigation.id)
        .where(
            Investigation.claim_group_id == group.id,
            Investigation.level == "N4",
            Investigation.stage != FAILED,
        )
        .limit(1)
    )
    if existing is not None:
        return None
    return await request_investigation(
        session,
        actor,
        group=group,
        content=content,
        level="N4",
        requested_by=requested_by,
        flags={"language_not_processed": True} if language_not_processed else None,
    )
