"""Reference facts and value bands (Q9): creation, validation, rejection, band validation.

A fact serves only once ``validated``; a band serves only while ``active`` and while its
fact is validated. Validating a fact computes the band of every group linked to it that
has none yet. A validated band never applies retroactively: inheritance happens for NEW
variants, decided by E3 (see docs/factcheck/pipeline.md).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.config import Settings, get_settings
from tower.core.domain import error_codes as ec
from tower.core.domain.errors import InvalidInput, NotFound
from tower.core.domain.factcheck.bands import band_of, tolerance
from tower.core.domain.factcheck.signatures import normalize_signature
from tower.core.domain.factcheck.values import (
    Band,
    BandSide,
    BandStatus,
    ClaimType,
    ContextNeed,
    ContextType,
    FactOrigin,
    FactStatus,
    GroupEventKind,
    Veracity,
    VerdictStatus,
)
from tower.core.domain.security import Actor, Permission
from tower.core.events.outbox import publish
from tower.core.factcheck import names
from tower.core.logging import get_logger
from tower.core.models.factcheck import Claim, ClaimGroup, Verdict
from tower.core.models.factcheck_v1 import ClaimGroupEvent, ReferenceFact, VerdictBand

__all__ = [
    "band_for_claim",
    "compute_group_bands",
    "create_fact_from_verdict",
    "create_reference_fact",
    "reject_reference_fact",
    "revoke_band",
    "validate_band",
    "validate_band_from_verdict",
    "validate_reference_fact",
]

log = get_logger(__name__)

_TERRITORY = re.compile(r"^[A-Z]{2}(-[A-Z0-9]{1,3})?$|^EU$|^INT$")
_INDICATOR = re.compile(r"^[a-z0-9][a-z0-9_.\-]{0,99}$")
_MAX_TEXT = 300


def _invalid(message: str, **details: object) -> InvalidInput:
    return InvalidInput(message, code=ec.FCK_FACT_INVALID, **details)


def _band_invalid(message: str, **details: object) -> InvalidInput:
    return InvalidInput(message, code=ec.FCK_BAND_INVALID, **details)


def _subject_key(text: str) -> str | None:
    """Same normalization as the signature subject key (text only, no QID)."""
    return normalize_signature(
        claim_type=ClaimType.STATISTIC.value,
        subject=text,
        subject_qid=None,
        predicate=None,
        value_low=None,
        value_high=None,
        unit=None,
        unit_scale=None,
        currency=None,
        value_date=None,
        period_start=None,
        period_end=None,
        place=None,
        primary_jurisdiction=None,
        polarity="affirmative",
        definition_variant=None,
        comparison_base=None,
        post_date=None,
    ).subject_key


def _scaled(
    low: Decimal | None, high: Decimal | None, unit: str | None, unit_scale: str | None
) -> tuple[Decimal | None, Decimal | None, str | None]:
    """Interval in the base unit (scale applied) and canonical unit."""
    sig = normalize_signature(
        claim_type=ClaimType.STATISTIC.value,
        subject=None,
        subject_qid=None,
        predicate=None,
        value_low=low,
        value_high=high,
        unit=unit,
        unit_scale=unit_scale,
        currency=None,
        value_date=None,
        period_start=None,
        period_end=None,
        place=None,
        primary_jurisdiction=None,
        polarity="affirmative",
        definition_variant=None,
        comparison_base=None,
        post_date=None,
    )
    return sig.value_low, sig.value_high, sig.unit


def band_for_claim(
    claim: Claim, fact: ReferenceFact, settings: Settings | None = None
) -> tuple[Band, BandSide] | None:
    """Band of the claimed interval against the fact value, ``None`` if the claim has no
    value or its unit differs from the fact's (never guess)."""
    settings = settings or get_settings()
    if claim.value_low is None and claim.value_high is None:
        return None
    low, high, unit = _scaled(claim.value_low, claim.value_high, claim.unit, claim.unit_scale)
    ref, _, fact_unit = _scaled(fact.value, None, fact.unit, fact.unit_scale)
    if ref is None or unit != fact_unit:
        return None
    tau = tolerance(
        ref,
        unit=fact_unit,
        ratio=Decimal(str(settings.fc_band_tau_ratio)),
        floor_rate_points=Decimal(str(settings.fc_band_floor_rate_points)),
        floor_default=Decimal(str(settings.fc_band_floor_default)),
    )
    return band_of(low, high, ref, tau)


async def _fact_by_public_id(
    session: AsyncSession, fact_id: uuid.UUID, *, lock: bool = False
) -> ReferenceFact:
    stmt = select(ReferenceFact).where(ReferenceFact.public_id == fact_id)
    if lock:
        stmt = stmt.with_for_update()
    fact = (await session.execute(stmt)).scalar_one_or_none()
    if fact is None:
        raise NotFound("Reference fact not found", code=ec.FCK_FACT_NOT_FOUND)
    return fact


async def _verdict_by_public_id(session: AsyncSession, verdict_id: uuid.UUID) -> Verdict:
    verdict = (
        await session.execute(select(Verdict).where(Verdict.public_id == verdict_id))
    ).scalar_one_or_none()
    if verdict is None:
        raise NotFound("Verdict not found", code=ec.FCK_VERDICT_NOT_FOUND)
    return verdict


async def _previous_fact(
    session: AsyncSession,
    *,
    indicator: str,
    territory: str,
    definition_variant: str | None,
    period_end: date,
) -> int | None:
    return await session.scalar(
        select(ReferenceFact.id)
        .where(
            ReferenceFact.indicator == indicator,
            ReferenceFact.territory == territory,
            ReferenceFact.definition_variant.is_not_distinct_from(definition_variant),
            ReferenceFact.status == FactStatus.VALIDATED.value,
            ReferenceFact.period_end < period_end,
        )
        .order_by(ReferenceFact.period_end.desc(), ReferenceFact.id.desc())
        .limit(1)
    )


async def create_reference_fact(
    session: AsyncSession,
    actor: Actor,
    *,
    indicator: str,
    label: str,
    value: Decimal,
    unit: str,
    unit_scale: str | None = None,
    period_start: date,
    period_end: date,
    territory: str,
    source_url: str,
    source_name: str,
    published_at: date,
    next_publication: date | None = None,
    definition_variant: str | None = None,
    subject_qid: str | None = None,
    aliases: Sequence[str] = (),
    previous_fact_id: uuid.UUID | None = None,
    origin: FactOrigin,
    origin_verdict_id: uuid.UUID | None = None,
    external_key: str | None = None,
) -> ReferenceFact:
    """New ``pending`` fact (a reviewer must validate it). Idempotent on ``external_key``."""
    actor.require(Permission.FACTS_WRITE)
    if external_key:
        existing = await session.scalar(
            select(ReferenceFact).where(ReferenceFact.external_key == external_key)
        )
        if existing is not None:
            return existing
    indicator = indicator.strip()
    label = label.strip()
    source_name = source_name.strip()
    territory = territory.strip().upper()
    if not _INDICATOR.fullmatch(indicator):
        raise _invalid("Invalid indicator key", indicator=indicator[:100])
    if not label or len(label) > _MAX_TEXT:
        raise _invalid("A label of at most 300 characters is required")
    if not source_name or len(source_name) > _MAX_TEXT:
        raise _invalid("A source name of at most 300 characters is required")
    if not isinstance(value, Decimal) or not value.is_finite():
        raise _invalid("The value must be a finite number")
    if period_start > period_end:
        raise _invalid(
            "The period start is after its end",
            period_start=period_start.isoformat(),
            period_end=period_end.isoformat(),
        )
    if not _TERRITORY.fullmatch(territory):
        raise _invalid("Invalid territory (ISO 3166-1/2 code, EU or INT)", territory=territory[:16])
    parsed = urlparse(source_url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise _invalid("The source URL must be http(s)")
    _, _, canonical_unit = _scaled(None, None, unit, unit_scale)
    if canonical_unit is None or "*" in canonical_unit:
        raise _invalid("Missing unit or unknown unit scale", unit=unit[:32])
    scale = unit_scale.strip().casefold() if unit_scale and unit_scale.strip() else None
    keys = [k for k in (_subject_key(a) for a in aliases) if k]
    clean_aliases = list(dict.fromkeys(keys))
    variant = definition_variant.strip().casefold() if definition_variant else None
    qid = subject_qid.strip().upper() if subject_qid and subject_qid.strip() else None

    previous_id: int | None
    if previous_fact_id is not None:
        previous = await _fact_by_public_id(session, previous_fact_id)
        previous_id = previous.id
    else:
        previous_id = await _previous_fact(
            session,
            indicator=indicator,
            territory=territory,
            definition_variant=variant,
            period_end=period_end,
        )
    origin_id: int | None = None
    if origin_verdict_id is not None:
        origin_id = (await _verdict_by_public_id(session, origin_verdict_id)).id

    fact = ReferenceFact(
        public_id=uuid.uuid4(),
        indicator=indicator,
        label=label,
        definition_variant=variant,
        subject_qid=qid,
        aliases=clean_aliases,
        value=value,
        unit=canonical_unit,
        unit_scale=scale,
        period_start=period_start,
        period_end=period_end,
        territory=territory,
        source_url=source_url.strip(),
        source_name=source_name,
        published_at=published_at,
        next_publication=next_publication,
        previous_fact_id=previous_id,
        status=FactStatus.PENDING.value,
        origin=origin.value,
        origin_verdict_id=origin_id,
        external_key=external_key,
        created_by=actor.label,
    )
    session.add(fact)
    await session.flush()
    log.info("reference_fact.created", fact=fact.id, indicator=indicator, origin=origin.value)
    return fact


async def _link_event(
    session: AsyncSession, actor: Actor, group: ClaimGroup, fact: ReferenceFact, reason: str
) -> None:
    session.add(
        ClaimGroupEvent(
            group_id=group.id,
            kind=GroupEventKind.FACT_LINKED.value,
            actor=actor.label,
            reason=reason,
            data={"reference_fact_id": str(fact.public_id), "indicator": fact.indicator},
        )
    )


async def create_fact_from_verdict(
    session: AsyncSession,
    actor: Actor,
    *,
    verdict_id: uuid.UUID,
    value: Decimal,
    period_start: date,
    period_end: date,
    source_url: str,
    source_name: str,
    published_at: date,
    next_publication: date | None = None,
    indicator: str | None = None,
    label: str | None = None,
) -> ReferenceFact:
    """Bot button "create a fact sheet" on an ACTIVE verdict of a statistic claim: the
    fact takes its unit, territory, definition and subject from the representative claim
    and the group is linked to it (the band is computed once the fact is validated)."""
    actor.require(Permission.FACTS_WRITE)
    verdict = await _verdict_by_public_id(session, verdict_id)
    if verdict.status != VerdictStatus.ACTIVE.value:
        raise _invalid("A fact can only be created from an active verdict", status=verdict.status)
    group = (
        await session.execute(
            select(ClaimGroup).where(ClaimGroup.id == verdict.claim_group_id).with_for_update()
        )
    ).scalar_one()
    claim = await session.get(Claim, group.representative_claim_id)
    if claim is None:  # pragma: no cover - foreign key
        raise _invalid("The group has no representative claim")
    if claim.claim_type != ClaimType.STATISTIC.value:
        raise _invalid("Only a statistic claim can give a reference fact", type=claim.claim_type)
    if group.reference_fact_id is not None:
        linked = await session.get(ReferenceFact, group.reference_fact_id)
        if linked is not None and linked.origin_verdict_id == verdict.id:
            return linked  # idempotent: the button was pressed twice
    territory = (claim.place or claim.primary_jurisdiction or "").strip().upper()
    if not territory or not claim.unit or not claim.predicate:
        raise _invalid("The claim lacks a territory, a unit or a predicate")
    predicate = claim.predicate.strip().casefold()
    key = indicator or f"{territory.lower()}.{predicate}"
    subject_alias = _subject_key(claim.subject) if claim.subject else None
    fact = await create_reference_fact(
        session,
        actor,
        indicator=key,
        label=label or claim.text_fr[:_MAX_TEXT],
        value=value,
        unit=claim.unit,
        unit_scale=claim.unit_scale,
        period_start=period_start,
        period_end=period_end,
        territory=territory,
        source_url=source_url,
        source_name=source_name,
        published_at=published_at,
        next_publication=next_publication,
        definition_variant=claim.definition_variant,
        subject_qid=claim.subject_qid,
        aliases=[subject_alias] if subject_alias else [],
        origin=FactOrigin.INVESTIGATION,
        origin_verdict_id=verdict_id,
    )
    if group.reference_fact_id is None:
        group.reference_fact_id = fact.id
        group.updated_at = func.now()
        await _link_event(session, actor, group, fact, "fact sheet created from the verdict")
    return fact


async def compute_group_bands(
    session: AsyncSession, fact: ReferenceFact, settings: Settings | None = None
) -> int:
    """Band of every group linked to ``fact`` that has none. Returns how many were set."""
    settings = settings or get_settings()
    groups = (
        await session.scalars(
            select(ClaimGroup)
            .where(ClaimGroup.reference_fact_id == fact.id, ClaimGroup.band.is_(None))
            .with_for_update()
        )
    ).all()
    count = 0
    for group in groups:
        claim = await session.get(Claim, group.representative_claim_id)
        if claim is None:  # pragma: no cover - foreign key
            continue
        result = band_for_claim(claim, fact, settings)
        if result is None:
            continue
        group.band, group.band_side = result[0].value, result[1].value
        group.updated_at = func.now()
        count += 1
    return count


async def validate_reference_fact(
    session: AsyncSession,
    actor: Actor,
    *,
    fact_id: uuid.UUID,
    settings: Settings | None = None,
) -> ReferenceFact:
    """pending -> validated, ``reference_fact.validated``, bands of the linked groups.
    Earlier facts of the series stay validated (older periods, ``outdated`` detection)."""
    actor.require(Permission.FACTS_VALIDATE)
    fact = await _fact_by_public_id(session, fact_id, lock=True)
    if fact.status == FactStatus.VALIDATED.value:
        return fact  # idempotent
    if fact.status != FactStatus.PENDING.value:
        raise _invalid("Only a pending fact can be validated", status=fact.status)
    fact.status = FactStatus.VALIDATED.value
    fact.validated_by = actor.label
    fact.validated_at = datetime.now(UTC)
    fact.updated_at = func.now()
    await session.flush()
    bands = await compute_group_bands(session, fact, settings)
    await publish(
        session,
        names.REFERENCE_FACT_VALIDATED,
        {
            "fact_id": str(fact.public_id),
            "indicator": fact.indicator,
            "territory": fact.territory,
            "period_start": fact.period_start.isoformat(),
            "period_end": fact.period_end.isoformat(),
            "validated_by": actor.label,
        },
        aggregate_type="reference_fact",
        aggregate_id=str(fact.public_id),
        actor=actor.label,
    )
    log.info("reference_fact.validated", fact=fact.id, bands_set=bands)
    return fact


async def reject_reference_fact(
    session: AsyncSession, actor: Actor, *, fact_id: uuid.UUID, reason: str
) -> ReferenceFact:
    """pending -> rejected (never served)."""
    actor.require(Permission.FACTS_VALIDATE)
    reason = reason.strip()
    if not reason:
        raise _invalid("A reason is required to reject a fact")
    fact = await _fact_by_public_id(session, fact_id, lock=True)
    if fact.status != FactStatus.PENDING.value:
        raise _invalid("Only a pending fact can be rejected", status=fact.status)
    fact.status = FactStatus.REJECTED.value
    fact.updated_at = func.now()
    await session.flush()
    log.info("reference_fact.rejected", fact=fact.id, by=actor.label, reason=reason[:200])
    return fact


async def validate_band(
    session: AsyncSession,
    actor: Actor,
    *,
    fact_id: uuid.UUID,
    band: Band,
    side: BandSide,
    veracity: Veracity,
    context_need: ContextNeed,
    context_types: Sequence[str] = (),
    source_verdict_id: uuid.UUID | None = None,
    reason: str | None = None,
) -> VerdictBand:
    """Validate the outcome of (fact, band, side) once; it will be inherited by new
    variants of the claim (never applied to existing groups). The previous active band
    of the same triple is revoked in the same transaction."""
    actor.require(Permission.FACTS_VALIDATE)
    fact = await _fact_by_public_id(session, fact_id, lock=True)
    if fact.status != FactStatus.VALIDATED.value:
        raise InvalidInput(
            "The reference fact is not validated",
            code=ec.FCK_FACT_NOT_VALIDATED,
            status=fact.status,
        )
    if (band is Band.CONTAINS) != (side is BandSide.NONE):
        raise _band_invalid("The side must be 'none' if and only if the band is 'contains'")
    valid_types = {t.value for t in ContextType}
    types = list(dict.fromkeys(context_types))
    if any(t not in valid_types for t in types):
        raise _band_invalid("Unknown context type")
    source_id: int | None = None
    if source_verdict_id is not None:
        source = await _verdict_by_public_id(session, source_verdict_id)
        group = await session.get(ClaimGroup, source.claim_group_id)
        if (
            source.status != VerdictStatus.ACTIVE.value
            or group is None
            or group.reference_fact_id != fact.id
        ):
            raise _band_invalid(
                "The source verdict must be an active verdict of a group of this fact"
            )
        source_id = source.id
    previous = (
        await session.scalars(
            select(VerdictBand)
            .where(
                VerdictBand.reference_fact_id == fact.id,
                VerdictBand.band == band.value,
                VerdictBand.side == side.value,
                VerdictBand.status == BandStatus.ACTIVE.value,
            )
            .with_for_update()
        )
    ).all()
    for old in previous:
        old.status = BandStatus.REVOKED.value
        old.revoked_by = actor.label
        old.revoked_at = datetime.now(UTC)
    await session.flush()  # free the unique "one active band per triple" slot
    row = VerdictBand(
        public_id=uuid.uuid4(),
        reference_fact_id=fact.id,
        band=band.value,
        side=side.value,
        veracity=veracity.value,
        context_need=context_need.value,
        context_types=types,
        source_verdict_id=source_id,
        status=BandStatus.ACTIVE.value,
        validated_by=actor.label,
        validated_at=datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    await publish(
        session,
        names.VERDICT_BAND_VALIDATED,
        {
            "band_id": str(row.public_id),
            "fact_id": str(fact.public_id),
            "band": band.value,
            "side": side.value,
            "veracity": veracity.value,
            "validated_by": actor.label,
        },
        aggregate_type="verdict_band",
        aggregate_id=str(row.public_id),
        actor=actor.label,
    )
    log.info(
        "verdict_band.validated",
        band_id=row.id,
        fact=fact.id,
        revoked=len(previous),
        reason=(reason or "")[:200] or None,
    )
    return row


async def validate_band_from_verdict(
    session: AsyncSession, actor: Actor, *, verdict_id: uuid.UUID
) -> VerdictBand:
    """Bot button "validate the band": the outcome is the active verdict's own veracity
    and context, for the band of its group against the group's validated fact."""
    actor.require(Permission.FACTS_VALIDATE)
    verdict = await _verdict_by_public_id(session, verdict_id)
    if verdict.status != VerdictStatus.ACTIVE.value:
        raise _band_invalid("The verdict is not active", status=verdict.status)
    if verdict.reuse_rule is not None:
        raise _band_invalid("An inherited verdict cannot define a band")
    group = await session.get(ClaimGroup, verdict.claim_group_id)
    if group is None or group.reference_fact_id is None:
        raise _band_invalid("The verdict's group has no reference fact")
    if group.band is None or group.band_side is None:
        raise _band_invalid("The verdict's group has no band")
    fact = await session.get(ReferenceFact, group.reference_fact_id)
    if fact is None:  # pragma: no cover - foreign key
        raise NotFound("Reference fact not found", code=ec.FCK_FACT_NOT_FOUND)
    return await validate_band(
        session,
        actor,
        fact_id=fact.public_id,
        band=Band(group.band),
        side=BandSide(group.band_side),
        veracity=Veracity(verdict.veracity),
        context_need=ContextNeed(verdict.context_need),
        context_types=list(verdict.context_types),
        source_verdict_id=verdict.public_id,
    )


async def revoke_band(
    session: AsyncSession, actor: Actor, *, band_id: uuid.UUID, reason: str
) -> VerdictBand:
    """Withdraw an active band: no new variant inherits from it any more."""
    actor.require(Permission.FACTS_VALIDATE)
    reason = reason.strip()
    if not reason:
        raise _band_invalid("A reason is required to revoke a band")
    row = (
        await session.execute(
            select(VerdictBand).where(VerdictBand.public_id == band_id).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Value band not found")
    if row.status != BandStatus.ACTIVE.value:
        raise _band_invalid("The band is already revoked")
    row.status = BandStatus.REVOKED.value
    row.revoked_by = actor.label
    row.revoked_at = datetime.now(UTC)
    await session.flush()
    log.info("verdict_band.revoked", band_id=row.id, by=actor.label, reason=reason[:200])
    return row
