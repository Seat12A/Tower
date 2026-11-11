"""Normalized claim signature and its deterministic comparison (E3).

Two claims are covered by the same verdict only if their signatures are identical.
When in doubt the comparison says "not identical": a wrong reuse is worse than a
missed one.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from tower.core.domain.factcheck.values import SignatureDifference

__all__ = [
    "NormalizedSignature",
    "Period",
    "SignatureComparison",
    "compare_signatures",
    "normalize_signature",
]

_SCALES: dict[str, Decimal] = {
    "thousand": Decimal(1000),
    "million": Decimal(1_000_000),
    "billion": Decimal(1_000_000_000),
}
_UNIT_ALIASES: dict[str, str] = {
    "%": "percent",
    "pct": "percent",
    "percent": "percent",
    "pourcent": "percent",
    "persons": "person",
    "people": "person",
    "personnes": "person",
    "eur": "EUR",
    "€": "EUR",
    "usd": "USD",
    "$": "USD",
}
_PUNCT = re.compile(r"[^\w\s]|_", re.UNICODE)
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class Period:
    start: date | None
    end: date | None
    current: bool  # "currently": no stated period/date, resolved at post_date
    post_date: date | None


@dataclass(frozen=True, slots=True)
class NormalizedSignature:
    claim_type: str
    subject_key: str | None
    predicate: str | None  # free text: not used for identity
    value_low: Decimal | None
    value_high: Decimal | None
    unit: str | None
    currency: str | None
    period: Period
    territory: str | None
    polarity: str
    definition_variant: str | None
    comparison_base: str | None


@dataclass(frozen=True, slots=True)
class SignatureComparison:
    identical: bool
    differences: frozenset[SignatureDifference]  # empty iff identical

    @property
    def conflicting(self) -> bool:
        """Any difference other than MISSING."""
        return any(d is not SignatureDifference.MISSING for d in self.differences)


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    text = text.strip()
    return text or None


def _subject_key(subject: str | None, qid: str | None) -> str | None:
    qid = _clean(qid)
    if qid:
        return qid.upper()
    subject = _clean(subject)
    if subject is None:
        return None
    decomposed = unicodedata.normalize("NFKD", subject)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    text = _PUNCT.sub(" ", stripped.casefold())
    text = _SPACES.sub(" ", text).strip()
    return text or None


def _decimal(value: Decimal | float | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _unit_and_scale(unit: str | None, scale: str | None) -> tuple[str | None, Decimal]:
    unit = _clean(unit)
    canonical = None
    if unit is not None:
        folded = unit.casefold()
        canonical = _UNIT_ALIASES.get(folded, folded)
    scale_key = _clean(scale)
    scale_key = scale_key.casefold() if scale_key else None
    if scale_key is None:
        return canonical, Decimal(1)
    if scale_key in _SCALES:
        return canonical, _SCALES[scale_key]
    # Unknown scale: never matches by accident.
    return f"{canonical or ''}*{scale_key}", Decimal(1)


def _upper(text: str | None) -> str | None:
    text = _clean(text)
    return text.upper() if text else None


def _lower(text: str | None) -> str | None:
    text = _clean(text)
    return text.casefold() if text else None


def normalize_signature(
    *,
    claim_type: str,
    subject: str | None,
    subject_qid: str | None,
    predicate: str | None,
    value_low: Decimal | float | None,
    value_high: Decimal | float | None,
    unit: str | None,
    unit_scale: str | None,
    currency: str | None,
    value_date: date | None,
    period_start: date | None,
    period_end: date | None,
    place: str | None,
    primary_jurisdiction: str | None,
    polarity: str,
    definition_variant: str | None,
    comparison_base: str | None,
    post_date: date | None,
) -> NormalizedSignature:
    canonical_unit, factor = _unit_and_scale(unit, unit_scale)
    low = _decimal(value_low)
    high = _decimal(value_high)
    if low is not None:
        low = low * factor
    if high is not None:
        high = high * factor

    if period_start is not None or period_end is not None:
        start = period_start if period_start is not None else period_end
        end = period_end if period_end is not None else period_start
        period = Period(start, end, False, post_date)
    elif value_date is not None:
        period = Period(value_date, value_date, False, post_date)
    else:
        period = Period(None, None, True, post_date)

    return NormalizedSignature(
        claim_type=claim_type,
        subject_key=_subject_key(subject, subject_qid),
        predicate=_lower(predicate),
        value_low=low,
        value_high=high,
        unit=canonical_unit,
        currency=_upper(currency),
        period=period,
        territory=_upper(place) or _upper(primary_jurisdiction),
        polarity=(_lower(polarity) or "affirmative"),
        definition_variant=_lower(definition_variant),
        comparison_base=_lower(comparison_base),
    )


def _optional(
    a: object | None, b: object | None, diff: SignatureDifference
) -> SignatureDifference | None:
    """Field that may be absent: absent on one side only is MISSING."""
    if a == b:
        return None
    if a is None or b is None:
        return SignatureDifference.MISSING
    return diff


def _compare_periods(a: Period, b: Period, current_period_days: int) -> SignatureDifference | None:
    if a.current and b.current:
        if a.post_date is None or b.post_date is None:
            return SignatureDifference.MISSING
        gap = abs((a.post_date - b.post_date).days)
        return None if gap <= current_period_days else SignatureDifference.PERIOD
    if a.current != b.current:
        current, explicit = (a, b) if a.current else (b, a)
        if current.post_date is None or explicit.start is None or explicit.end is None:
            return SignatureDifference.MISSING
        if explicit.start <= current.post_date <= explicit.end:
            return SignatureDifference.MISSING
        return SignatureDifference.PERIOD
    if a.start == b.start and a.end == b.end:
        return None
    return SignatureDifference.PERIOD


def compare_signatures(
    a: NormalizedSignature, b: NormalizedSignature, *, current_period_days: int
) -> SignatureComparison:
    diffs: set[SignatureDifference] = set()

    if a.claim_type != b.claim_type:
        diffs.add(SignatureDifference.CLAIM_TYPE)
    found = _optional(a.subject_key, b.subject_key, SignatureDifference.SUBJECT)
    if found:
        diffs.add(found)
    if (a.value_low, a.value_high) != (b.value_low, b.value_high):
        diffs.add(SignatureDifference.VALUE)
    if a.unit != b.unit:
        diffs.add(SignatureDifference.UNIT)
    for x, y, diff in (
        (a.currency, b.currency, SignatureDifference.CURRENCY),
        (a.territory, b.territory, SignatureDifference.TERRITORY),
        (a.definition_variant, b.definition_variant, SignatureDifference.DEFINITION),
        (a.comparison_base, b.comparison_base, SignatureDifference.COMPARISON_BASE),
    ):
        found = _optional(x, y, diff)
        if found:
            diffs.add(found)
    if a.polarity != b.polarity:
        diffs.add(SignatureDifference.POLARITY)
    found = _compare_periods(a.period, b.period, current_period_days)
    if found:
        diffs.add(found)

    return SignatureComparison(identical=not diffs, differences=frozenset(diffs))
