"""Usage report (free allocation of the day, monthly spend per scope): single source for
``tower llm usage`` and ``GET /v1/llm/usage``."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.config import Settings

__all__ = ["FreeUsageLine", "ScopeSpend", "UsageReport", "get_usage"]


@dataclass(frozen=True, slots=True)
class FreeUsageLine:
    provider: str
    free_group: str
    used_tokens: int
    allowance_tokens: int


@dataclass(frozen=True, slots=True)
class ScopeSpend:
    scope: str
    spent_usd_micros: int
    reserved_usd_micros: int
    cap_usd_micros: int


@dataclass(frozen=True, slots=True)
class UsageReport:
    day: date
    """UTC day of the free allocation."""
    usage_tier: int
    free: tuple[FreeUsageLine, ...]
    period_start: date
    period_end: date
    scopes: tuple[ScopeSpend, ...]
    """Every counter of the month, sorted by scope (``global``, ``provider:x``...)."""


async def get_usage(session: AsyncSession, settings: Settings, now: datetime) -> UsageReport:
    raise NotImplementedError
