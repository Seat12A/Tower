"""Usage report (free allocation of the day, monthly spend per scope): single source for
``tower llm usage`` and ``GET /v1/llm/usage``."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.config import Settings
from tower.core.domain.llm import FreeGroup, free_allowance, month_period

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
    day = now.date()
    start, end = month_period(now)
    used = {
        (r.provider, r.free_group): int(r.tokens)
        for r in (
            await session.execute(
                text("SELECT provider, free_group, tokens FROM llm_free_usage WHERE day = :day"),
                {"day": day},
            )
        ).all()
    }
    rows = (
        await session.execute(
            text(
                "SELECT scope, spent_usd_micros, reserved_usd_micros, cap_usd_micros"
                " FROM budget_counters WHERE period_start = :p ORDER BY scope"
            ),
            {"p": start},
        )
    ).all()
    return UsageReport(
        day=day,
        usage_tier=settings.openai_usage_tier,
        free=tuple(
            FreeUsageLine(
                "openai",
                group.value,
                used.get(("openai", group.value), 0),
                free_allowance(settings.openai_usage_tier, group),
            )
            for group in FreeGroup
        ),
        period_start=start,
        period_end=end,
        scopes=tuple(
            ScopeSpend(
                r.scope,
                int(r.spent_usd_micros),
                int(r.reserved_usd_micros),
                int(r.cap_usd_micros),
            )
            for r in rows
        ),
    )
