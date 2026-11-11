"""Reference facts and value bands: read-only resolution for E3 (Q9).

Only ``validated`` facts and ``active`` bands of validated facts are ever served.
Resolution is deterministic (no model): subject, unit, territory, definition, period.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.config import Settings
from tower.core.domain.factcheck.bands import band_of, is_outdated, tolerance
from tower.core.domain.factcheck.signatures import NormalizedSignature
from tower.core.domain.factcheck.values import Band, BandSide, BandStatus, FactStatus
from tower.core.models.factcheck_v1 import ReferenceFact, VerdictBand

__all__ = ["active_band", "band_for", "fact_value", "resolve_fact"]

_SCALES = {
    "thousand": Decimal(1000),
    "million": Decimal(1_000_000),
    "billion": Decimal(1_000_000_000),
}
_PUNCT = re.compile(r"[^\w\s]|_", re.UNICODE)
_SPACES = re.compile(r"\s+")


def _normalize(text: str | None) -> str | None:
    """Same normalization as the signature ``subject_key`` (accent-stripped, casefolded)."""
    if text is None or not text.strip():
        return None
    stripped = "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )
    folded = _SPACES.sub(" ", _PUNCT.sub(" ", stripped.casefold())).strip()
    return folded or None


def fact_value(fact: ReferenceFact) -> Decimal:
    """Value of the fact in base unit (after ``unit_scale``)."""
    factor = _SCALES.get((fact.unit_scale or "").casefold(), Decimal(1))
    return Decimal(fact.value) * factor


def _subject_matches(sig: NormalizedSignature, fact: ReferenceFact) -> bool:
    key = sig.subject_key
    if key is not None:
        if fact.subject_qid and key == fact.subject_qid.strip().upper():
            return True
        if key in {_normalize(a) for a in fact.aliases}:
            return True
    predicate = sig.predicate
    suffix = fact.indicator.rsplit(".", 1)[-1].casefold()
    return predicate is not None and predicate.casefold() == suffix


def _has_value(sig: NormalizedSignature) -> bool:
    return sig.value_low is not None or sig.value_high is not None


async def resolve_fact(
    session: AsyncSession, sig: NormalizedSignature, *, post_date: date | None
) -> ReferenceFact | None:
    """The validated reference fact a numeric claim is about, or ``None``.

    Subject (QID, alias, or indicator suffix == predicate), unit, territory and
    definition variant must agree; an explicit claim period must lie within the fact
    period; a "current" claim takes the latest fact published at the post date."""
    if not _has_value(sig) or sig.unit is None or sig.territory is None:
        return None
    query = select(ReferenceFact).where(
        ReferenceFact.status == FactStatus.VALIDATED.value,
        func.lower(ReferenceFact.unit) == sig.unit.casefold(),
        func.upper(ReferenceFact.territory) == sig.territory.upper(),
    )
    if sig.definition_variant is None:
        query = query.where(ReferenceFact.definition_variant.is_(None))
    else:
        query = query.where(func.lower(ReferenceFact.definition_variant) == sig.definition_variant)
    period = sig.period
    if period.current:
        if post_date is not None:
            query = query.where(ReferenceFact.published_at <= post_date)
    else:
        if period.start is None or period.end is None:
            return None
        query = query.where(
            ReferenceFact.period_start <= period.start, ReferenceFact.period_end >= period.end
        )
    query = query.order_by(ReferenceFact.published_at.desc(), ReferenceFact.id.desc())
    for fact in (await session.scalars(query)).all():
        if _subject_matches(sig, fact):
            return fact
    return None


async def active_band(
    session: AsyncSession, fact_id: int, band: Band | str, side: BandSide | str
) -> VerdictBand | None:
    """The active band row of a validated fact, if any."""
    return await session.scalar(
        select(VerdictBand)
        .join(ReferenceFact, ReferenceFact.id == VerdictBand.reference_fact_id)
        .where(
            VerdictBand.reference_fact_id == fact_id,
            VerdictBand.band == str(band),
            VerdictBand.side == str(side),
            VerdictBand.status == BandStatus.ACTIVE.value,
            ReferenceFact.status == FactStatus.VALIDATED.value,
        )
    )


async def band_for(
    session: AsyncSession, sig: NormalizedSignature, fact: ReferenceFact, settings: Settings
) -> tuple[Band, BandSide, bool]:
    """Band and side of the claimed interval against ``fact``, and whether the claim
    matches the previous (validated) value of the indicator (``outdated``)."""
    reference = fact_value(fact)
    tau = tolerance(
        reference,
        unit=sig.unit,
        ratio=Decimal(str(settings.fc_band_tau_ratio)),
        floor_rate_points=Decimal(str(settings.fc_band_floor_rate_points)),
        floor_default=Decimal(str(settings.fc_band_floor_default)),
    )
    band, side = band_of(sig.value_low, sig.value_high, reference, tau)
    previous: Decimal | None = None
    if fact.previous_fact_id is not None:
        prev = await session.get(ReferenceFact, fact.previous_fact_id)
        if prev is not None and prev.status == FactStatus.VALIDATED.value:
            previous = fact_value(prev)
    outdated = is_outdated(
        sig.value_low, sig.value_high, current=reference, previous=previous, tau=tau
    )
    return band, side, outdated
