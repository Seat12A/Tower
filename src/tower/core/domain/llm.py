"""Model router rules: levels, cache keys, costs, free allocation, budgets.

Standard library only. Prices are stored in US dollars per million tokens, so a cost
in micro-dollars is simply ``tokens × price``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
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
    raise NotImplementedError


def estimate_usage(input_chars: int, max_output_tokens: int) -> TokenUsage:
    """Upper-bound estimate before a call: input = ceil(chars / CHARS_PER_TOKEN) +
    ESTIMATE_OVERHEAD_TOKENS, output = max_output_tokens, cached = 0."""
    raise NotImplementedError


def estimate_tokens(input_chars: int, max_output_tokens: int) -> int:
    """``estimate_usage(...).total``."""
    raise NotImplementedError


def cost_usd_micros(usage: TokenUsage, prices: ModelPrices) -> int:
    """(input - cached) × input price + cached × cached price + output × output price,
    rounded UP to an integer number of micro-dollars."""
    raise NotImplementedError


def free_allowance(tier: int, group: FreeGroup) -> int:
    """Daily free tokens: tiers 1-2 → small 2_500_000, large 250_000; tiers 3-5 →
    small 10_000_000, large 1_000_000. Other tiers → ``ValueError``."""
    raise NotImplementedError


def can_use_free(used: int, estimate: int, allowance: int, margin: float = 0.10) -> bool:
    """True if ``allowance - used >= estimate × (1 + margin)``: a request that would
    exceed the remaining allocation is billed in full, so never risk it."""
    raise NotImplementedError


def crossed_thresholds(before_micros: int, after_micros: int, cap_micros: int) -> list[int]:
    """Thresholds of ``BUDGET_THRESHOLDS`` (percent of ``cap_micros``) crossed going from
    ``before`` to ``after`` (``before < t% ≤ after``). Cap ≤ 0 → []."""
    raise NotImplementedError


def month_period(now: datetime) -> tuple[date, date]:
    """``(first day of the UTC month of now, first day of the next month)``."""
    raise NotImplementedError
