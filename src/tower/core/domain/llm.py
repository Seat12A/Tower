"""Model router rules: levels, cache keys, costs, free allocation, budgets.

Standard library only. Prices are stored in US dollars per million tokens, so a cost
in micro-dollars is simply ``tokens × price``.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from typing import Any


class Level(StrEnum):
    N0 = "N0"
    N1 = "N1"
    N2 = "N2"
    N3 = "N3"
    N4 = "N4"


ANY_LEVEL = "*"
"""Route level that matches every level (used when no level-specific route exists)."""


class Provider(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


class FreeGroup(StrEnum):
    """OpenAI free allocation groups (data sharing programme)."""

    SMALL = "small"
    LARGE = "large"


class CallStatus(StrEnum):
    SUCCEEDED = "succeeded"
    INVALID_OUTPUT = "invalid_output"
    REFUSED = "refused"
    FAILED = "failed"


BUDGET_THRESHOLDS: tuple[int, ...] = (50, 80, 100)
GLOBAL_SCOPE = "global"
CHARS_PER_TOKEN = 3
"""Conservative estimate (multilingual text, JSON): real ratios are ~3.5-4 for English."""
ESTIMATE_OVERHEAD_TOKENS = 50
"""Per-request overhead (message framing, schema reference)."""


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int = 0
    """All input tokens, cached ones included."""
    cached_input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class ModelPrices:
    """US dollars per million tokens."""

    input: Decimal
    cached_input: Decimal
    output: Decimal


def cache_key(
    role: str,
    provider: str,
    model: str,
    prompt_version: str,
    params: Mapping[str, Any],
    input: Mapping[str, Any],
) -> str:
    """Hex SHA-256 of canonical JSON of all arguments
    (``json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)``).
    Raises ``TypeError`` for non JSON-serialisable values."""
    payload = {
        "role": role,
        "provider": provider,
        "model": model,
        "prompt_version": prompt_version,
        "params": dict(params),
        "input": dict(input),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def estimate_usage(input_chars: int, max_output_tokens: int) -> TokenUsage:
    """Upper-bound estimate before a call: input = ceil(chars / CHARS_PER_TOKEN) +
    ESTIMATE_OVERHEAD_TOKENS, output = max_output_tokens, cached = 0."""
    input_tokens = math.ceil(input_chars / CHARS_PER_TOKEN) + ESTIMATE_OVERHEAD_TOKENS
    return TokenUsage(
        input_tokens=input_tokens, cached_input_tokens=0, output_tokens=max_output_tokens
    )


def estimate_tokens(input_chars: int, max_output_tokens: int) -> int:
    """``estimate_usage(...).total``."""
    return estimate_usage(input_chars, max_output_tokens).total


def cost_usd_micros(usage: TokenUsage, prices: ModelPrices) -> int:
    """(input - cached) × input price + cached × cached price + output × output price,
    rounded UP to an integer number of micro-dollars."""
    cached = min(usage.cached_input_tokens, usage.input_tokens)
    total = (
        (usage.input_tokens - cached) * prices.input
        + cached * prices.cached_input
        + usage.output_tokens * prices.output
    )
    return int(total.to_integral_value(rounding=ROUND_CEILING))


def free_allowance(tier: int, group: FreeGroup) -> int:
    """Daily free tokens: tiers 1-2 → small 2_500_000, large 250_000; tiers 3-5 →
    small 10_000_000, large 1_000_000. Other tiers → ``ValueError``."""
    if tier in (1, 2):
        return 2_500_000 if group is FreeGroup.SMALL else 250_000
    if tier in (3, 4, 5):
        return 10_000_000 if group is FreeGroup.SMALL else 1_000_000
    raise ValueError(f"Unsupported usage tier: {tier}")


def can_use_free(used: int, estimate: int, allowance: int, margin: float = 0.10) -> bool:
    """True if ``allowance - used >= estimate × (1 + margin)``: a request that would
    exceed the remaining allocation is billed in full, so never risk it."""
    needed = Decimal(estimate) * (1 + Decimal(str(margin)))
    return Decimal(allowance - used) >= needed


def crossed_thresholds(before_micros: int, after_micros: int, cap_micros: int) -> list[int]:
    """Thresholds of ``BUDGET_THRESHOLDS`` (percent of ``cap_micros``) crossed going from
    ``before`` to ``after`` (``before < t% ≤ after``). Cap ≤ 0 → []."""
    if cap_micros <= 0:
        return []
    return [
        t for t in BUDGET_THRESHOLDS if before_micros * 100 < t * cap_micros <= after_micros * 100
    ]


def month_period(now: datetime) -> tuple[date, date]:
    """``(first day of the UTC month of now, first day of the next month)``."""
    moment = now.astimezone(UTC) if now.tzinfo is not None else now
    start = date(moment.year, moment.month, 1)
    end = (
        date(moment.year + 1, 1, 1)
        if moment.month == 12
        else date(moment.year, moment.month + 1, 1)
    )
    return start, end
