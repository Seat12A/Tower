"""``tower llm seed`` / ``tower llm usage``: models, default routes, budget; consumption."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain.llm import GLOBAL_SCOPE, month_period
from tower.core.llm.usage import get_usage
from tower.core.models.llm import BudgetCounter, LLMModel, LLMRoute

__all__ = ["run_budget", "run_seed", "run_usage"]

PRICES_CHECKED_ON = date(2026, 11, 10)
GLOBAL_CAP_USD_MICROS = 30_000_000
SEED_ACTOR = "system:seed"
X_SCOPE = "provider:x"


@dataclass(frozen=True, slots=True)
class _ModelSeed:
    provider: str
    model: str
    input: str
    cached: str
    output: str
    free_group: str | None = None


# $/Mtok. OpenAI cached input = 10 % of input.
_MODELS: tuple[_ModelSeed, ...] = (
    _ModelSeed("openai", "gpt-5-nano", "0.05", "0.005", "0.40", "small"),
    _ModelSeed("openai", "gpt-5-mini", "0.25", "0.025", "2", "small"),
    _ModelSeed("openai", "gpt-5.6-luna", "0.20", "0.02", "1.20", "small"),
    _ModelSeed("openai", "gpt-5.6-terra", "2", "0.20", "12", "small"),
    _ModelSeed("openai", "gpt-5.6-sol", "4", "0.40", "20", "large"),
    _ModelSeed("openai", "gpt-6-luna", "0.10", "0.01", "0.50"),
    _ModelSeed("openai", "gpt-6.1-terra", "1", "0.10", "5"),
    _ModelSeed("openai", "text-embedding-3-small", "0.02", "0.02", "0"),
    _ModelSeed("anthropic", "claude-haiku-5-5", "0.10", "0.01", "0.50"),
    _ModelSeed("anthropic", "claude-sonnet-5-5", "2", "0.10", "10"),
    _ModelSeed("anthropic", "claude-opus-5-5", "4", "0.20", "20"),
    _ModelSeed("anthropic", "claude-fable-5-2", "8", "0.20", "40"),
)


@dataclass(frozen=True, slots=True)
class _RouteSeed:
    role: str
    level: str
    model: str
    fallbacks: tuple[str, ...]
    params: dict[str, Any] = field(default_factory=dict)
    data_sharing_ok: bool = False


def _low(
    role: str, model: str, *fallbacks: str, level: str = "*", shared: bool = True
) -> _RouteSeed:
    return _RouteSeed(role, level, model, fallbacks, {"effort": "low"}, shared)


def _effort(role: str, level: str, effort: str, model: str, *fallbacks: str) -> _RouteSeed:
    return _RouteSeed(role, level, model, fallbacks, {"effort": effort}, False)


_ROUTES: tuple[_RouteSeed, ...] = (
    _low("extract", "gpt-5.6-luna", "gpt-6-luna", "claude-haiku-5-5"),
    _low("pair_arbiter", "gpt-5-nano", "gpt-6-luna"),
    _low("light_check", "gpt-5.6-terra", "gpt-6.1-terra", "claude-haiku-5-5"),
    _low("query_writer", "gpt-5.6-terra", "gpt-6-luna"),
    _low("narrative_title", "gpt-5.6-luna", "gpt-6-luna"),
    _low("translate", "gpt-5.6-luna", "gpt-6-luna", "claude-haiku-5-5"),
    _effort("frame", "*", "medium", "claude-sonnet-5-5"),
    _effort("frame", "N4", "medium", "claude-opus-5-5", "claude-sonnet-5-5"),
    _effort("judge", "*", "high", "claude-sonnet-5-5"),
    _effort("judge", "N4", "medium", "claude-opus-5-5", "claude-sonnet-5-5"),
    # Reinforced depth (escalation, and always in N4): Opus, Sonnet as fallback.
    _effort("frame_reinforced", "*", "medium", "claude-opus-5-5", "claude-sonnet-5-5"),
    _effort("judge_reinforced", "*", "medium", "claude-opus-5-5", "claude-sonnet-5-5"),
    *(
        _RouteSeed(role, "*", "gpt-5.6-sol", ("gpt-6.1-terra",), {"effort": "medium"}, True)
        for role in ("second_opinion", "objection", "critic")
    ),
    _effort("writer", "*", "medium", "claude-sonnet-5-5", "claude-haiku-5-5"),
    _effort("auditor", "*", "high", "claude-fable-5-2", "claude-opus-5-5"),
    _RouteSeed("embed", "*", "text-embedding-3-small", (), {"dimensions": 512}, False),
)


async def _seed(db: Database, settings: Settings, now: datetime) -> dict[str, int]:
    counts = {
        "models": 0,
        "routes_created": 0,
        "routes_kept": 0,
        "budget_created": 0,
        "x_budget_created": 0,
    }
    async with db.transaction() as session:
        for m in _MODELS:
            stmt = pg_insert(LLMModel).values(
                provider=m.provider,
                model=m.model,
                input_usd_per_mtok=Decimal(m.input),
                cached_input_usd_per_mtok=Decimal(m.cached),
                output_usd_per_mtok=Decimal(m.output),
                batch_factor=Decimal("0.5"),
                free_group=m.free_group,
                prices_checked_on=PRICES_CHECKED_ON,
            )
            # free_group is only set on insert: a team that marked a model as free (or
            # not) in the database keeps its choice; prices are refreshed.
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[LLMModel.provider, LLMModel.model],
                    set_={
                        "input_usd_per_mtok": stmt.excluded.input_usd_per_mtok,
                        "cached_input_usd_per_mtok": stmt.excluded.cached_input_usd_per_mtok,
                        "output_usd_per_mtok": stmt.excluded.output_usd_per_mtok,
                        "batch_factor": stmt.excluded.batch_factor,
                        "prices_checked_on": stmt.excluded.prices_checked_on,
                        "updated_at": text("now()"),
                    },
                )
            )
            counts["models"] += 1

        ids = {
            row.model: row.id
            for row in (await session.execute(text("SELECT id, model FROM llm_models"))).all()
        }
        for r in _ROUTES:
            exists = (
                await session.execute(
                    text("SELECT 1 FROM llm_routes WHERE role = :role AND level = :level LIMIT 1"),
                    {"role": r.role, "level": r.level},
                )
            ).first()
            if exists is not None:
                counts["routes_kept"] += 1
                continue
            await session.execute(
                pg_insert(LLMRoute).values(
                    role=r.role,
                    level=r.level,
                    model_id=ids[r.model],
                    fallback_model_ids=[ids[f] for f in r.fallbacks],
                    params=r.params,
                    data_sharing_ok=r.data_sharing_ok,
                    version=1,
                    created_by=SEED_ACTOR,
                )
            )
            counts["routes_created"] += 1

        start, end = month_period(now)
        has_global = (
            await session.execute(
                text("SELECT 1 FROM budget_counters WHERE scope = :s AND period_start = :p"),
                {"s": GLOBAL_SCOPE, "p": start},
            )
        ).first()
        if has_global is None:
            await session.execute(
                pg_insert(BudgetCounter)
                .values(
                    scope=GLOBAL_SCOPE,
                    period_start=start,
                    period_end=end,
                    cap_usd_micros=GLOBAL_CAP_USD_MICROS,
                )
                .on_conflict_do_nothing(index_elements=["scope", "period_start"])
            )
            counts["budget_created"] = 1
        # Provider X counter: created only when the scope has no counter at all, so a
        # cap changed by the team (or a previous month's counter) is never overwritten.
        has_x = (
            await session.execute(
                text("SELECT 1 FROM budget_counters WHERE scope = :s LIMIT 1"),
                {"s": X_SCOPE},
            )
        ).first()
        if has_x is None:
            await session.execute(
                pg_insert(BudgetCounter)
                .values(
                    scope=X_SCOPE,
                    period_start=start,
                    period_end=end,
                    cap_usd_micros=round(settings.x_monthly_cap_usd * 1_000_000),
                )
                .on_conflict_do_nothing(index_elements=["scope", "period_start"])
            )
            counts["x_budget_created"] = 1
    return counts


async def run_seed(settings: Settings) -> None:
    db = Database(settings)
    try:
        counts = await _seed(db, settings, datetime.now(UTC))
    finally:
        await db.dispose()
    print(
        f"Models upserted: {counts['models']}. "
        f"Routes created: {counts['routes_created']} (kept: {counts['routes_kept']}). "
        f"Global budget created: {'yes' if counts['budget_created'] else 'no (already set)'}. "
        f"Provider X budget created: {'yes' if counts['x_budget_created'] else 'no (already set)'}."
    )


def _usd(micros: int) -> str:
    return f"${micros / 1_000_000:,.2f}"


async def run_usage(settings: Settings) -> None:
    db = Database(settings)
    try:
        async with db.session() as session:
            report = await get_usage(session, settings, datetime.now(UTC))
    finally:
        await db.dispose()

    print(f"Free allocation, {report.day.isoformat()} (UTC), tier {report.usage_tier}")
    for line in report.free:
        print(
            f"  {line.provider}/{line.free_group:<6} {line.used_tokens:>12,} /"
            f" {line.allowance_tokens:>12,} tokens"
            f"  {100 * line.used_tokens / line.allowance_tokens:5.1f}%"
        )
    print(f"Budget, period starting {report.period_start.isoformat()}")
    if not report.scopes:
        print("  (no budget counter: run `tower llm seed`)")
    for row in report.scopes:
        print(
            f"  {row.scope:<16} spent {_usd(row.spent_usd_micros):>10}"
            f"  reserved {_usd(row.reserved_usd_micros):>10}  cap {_usd(row.cap_usd_micros):>10}"
        )


_SCOPE = re.compile(r"^[a-z0-9_]+:[a-z0-9_.-]+$")


async def run_budget(settings: Settings, scope: str, usd: float) -> None:
    """Set the monthly cap of ``scope`` for the current period (creates the counter)."""
    if scope != GLOBAL_SCOPE and not _SCOPE.fullmatch(scope):
        raise ValueError(f"Invalid scope {scope!r} (expected 'global' or '<kind>:<name>')")
    if not math.isfinite(usd) or usd < 0:
        raise ValueError("Cap must be a finite amount >= 0 (USD)")
    cap = round(usd * 1_000_000)
    start, end = month_period(datetime.now(UTC))
    db = Database(settings)
    try:
        async with db.transaction() as session:
            stmt = pg_insert(BudgetCounter).values(
                scope=scope, period_start=start, period_end=end, cap_usd_micros=cap
            )
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["scope", "period_start"],
                    set_={
                        "cap_usd_micros": stmt.excluded.cap_usd_micros,
                        "updated_at": text("now()"),
                    },
                )
            )
    finally:
        await db.dispose()
    print(f"Budget cap of {scope} for the period starting {start.isoformat()}: {_usd(cap)}")
