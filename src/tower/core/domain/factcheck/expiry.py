"""Verdict expiry: default lifetimes per claim type and the expiry computation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta

from tower.core.domain.factcheck.values import ClaimType

__all__ = [
    "DEFAULT_TTL_DAYS",
    "PROVISIONAL_CAP",
    "parse_ttl_overrides",
    "ttl_days",
    "verdict_expiry",
]

DEFAULT_TTL_DAYS: Mapping[ClaimType, int] = {
    ClaimType.STATISTIC: 120,
    ClaimType.HISTORICAL_FACT: 3650,
    ClaimType.QUOTE: 3650,
    ClaimType.LEGAL_TEXT: 180,
    ClaimType.ONGOING_SITUATION: 2,
    ClaimType.PROCEDURE: 14,
    ClaimType.RECORD: 365,
    ClaimType.CONDUCT: 365,
    ClaimType.ACCUSATION: 90,
    ClaimType.PREDICTION: 30,
    ClaimType.OPINION: 30,
    ClaimType.CAUSAL: 365,
    ClaimType.OTHER: 180,
}

PROVISIONAL_CAP = timedelta(hours=6)


def ttl_days(claim_type: ClaimType, overrides: Mapping[str, int] | None = None) -> int:
    """Lifetime in days for a claim type, honouring operator overrides."""
    if overrides and claim_type.value in overrides:
        return overrides[claim_type.value]
    return DEFAULT_TTL_DAYS[claim_type]


def verdict_expiry(
    *,
    validated_at: datetime,
    claim_type: ClaimType,
    overrides: Mapping[str, int] | None = None,
    next_publication: date | None = None,
    has_provisional_note: bool = False,
) -> datetime:
    """Earliest of: TTL, next publication of the reference (00:00 UTC), 6 h if provisional."""
    candidates = [validated_at + timedelta(days=ttl_days(claim_type, overrides))]
    if next_publication is not None:
        candidates.append(datetime.combine(next_publication, time.min, tzinfo=validated_at.tzinfo))
    if has_provisional_note:
        candidates.append(validated_at + PROVISIONAL_CAP)
    return min(candidates)


def parse_ttl_overrides(raw: str) -> dict[str, int]:
    """Parse ``TOWER_FC_VERDICT_TTL_DAYS`` (JSON object ``{claim_type: days}``)."""
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid TTL overrides JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise ValueError("TTL overrides must be a JSON object")
    valid = {c.value for c in ClaimType}
    result: dict[str, int] = {}
    for key, value in data.items():
        if key not in valid:
            raise ValueError(f"unknown claim type in TTL overrides: {key!r}")
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"TTL for {key!r} must be a positive integer number of days")
        result[key] = value
    return result
