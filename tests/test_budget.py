from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from tests.test_llm_router import Answer, FakeProvider, add_model, add_route
from tower.core.budget import (
    BudgetRefused,
    release_expired,
    reserve_budget,
    reserve_free,
    settle_budget,
    settle_free,
)
from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain.llm import Level
from tower.core.jobs.maintenance import run_maintenance, run_singleton
from tower.core.llm.prompts import PromptRegistry
from tower.core.llm.router import LLMRouter

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
PERIOD = date(2026, 11, 1)
TTL = 600


async def add_counter(
    db: Database, scope: str, cap: int, *, start: date = PERIOD, end: date = date(2026, 12, 1)
) -> None:
    async with db.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO budget_counters (scope, period_start, period_end, cap_usd_micros)"
                " VALUES (:s, :ps, :pe, :c)"
            ),
            {"s": scope, "ps": start, "pe": end, "c": cap},
        )


async def amounts(db: Database, scope: str, start: date = PERIOD) -> tuple[int, int]:
    async with db.session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT spent_usd_micros, reserved_usd_micros FROM budget_counters"
                    " WHERE scope = :s AND period_start = :ps"
                ),
                {"s": scope, "ps": start},
            )
        ).one()
    return int(row[0]), int(row[1])


async def n_rows(db: Database, kind: str | None = None) -> int:
    async with db.session() as session:
        return int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM budget_reservations"
                        " WHERE CAST(:k AS text) IS NULL OR kind = :k"
                    ),
                    {"k": kind},
                )
            ).scalar_one()
        )


async def reserve(db: Database, *scopes: str, amount: int = 100) -> Any:
    return await reserve_budget(db, scopes, amount, ref="test", now=NOW, ttl_s=TTL)


async def settle(db: Database, holds: Any, actual: int) -> None:
    async with db.transaction() as session:
        await settle_budget(session, holds, actual, actor="test")


async def release(db: Database, now: datetime) -> int:
    async with db.transaction() as session:
        return await release_expired(session, now)


async def test_reserve_then_settle(db: Database) -> None:
    await add_counter(db, "global", 1000)
    holds = await reserve(db, "global", "global")
    assert len(holds) == 1  # deduplicated
    assert await amounts(db, "global") == (0, 100)
    assert await n_rows(db, "budget") == 1
    await settle(db, holds, 70)
    assert await amounts(db, "global") == (70, 0)
    assert await n_rows(db) == 0


async def test_refusal_is_all_or_nothing(db: Database) -> None:
    await add_counter(db, "global", 1000)
    await add_counter(db, "provider:x", 50)
    with pytest.raises(BudgetRefused) as info:
        await reserve(db, "provider:x", "global")
    assert info.value.scope == "provider:x"
    assert info.value.retry_at == datetime(2026, 12, 1, tzinfo=UTC)
    assert await amounts(db, "global") == (0, 0)
    assert await amounts(db, "provider:x") == (0, 0)
    assert await n_rows(db) == 0


async def test_uncapped_scope_has_no_hold(db: Database) -> None:
    await add_counter(db, "global", 1000)
    holds = await reserve(db, "global", "nocounter")
    assert [h.scope for h in holds] == ["global"]
    assert await reserve(db, "nocounter") == ()


async def test_zero_amount_touches_nothing(db: Database) -> None:
    await add_counter(db, "global", 1000)
    assert await reserve(db, "global", amount=0) == ()
    assert await amounts(db, "global") == (0, 0)
    assert await n_rows(db) == 0


async def test_month_rollover_creates_counter(db: Database) -> None:
    await add_counter(db, "global", 1000, start=date(2026, 10, 1), end=date(2026, 11, 1))
    holds = await reserve(db, "global")
    assert holds[0].period_start == PERIOD
    assert await amounts(db, "global") == (0, 100)
    assert await amounts(db, "global", date(2026, 10, 1)) == (0, 0)


async def test_orphan_reservation_is_released_and_late_settle_is_safe(db: Database) -> None:
    await add_counter(db, "global", 10_000)
    orphan = await reserve(db, "global", amount=100)  # process "killed": never settled
    live = await reserve(db, "global", amount=300)
    assert await amounts(db, "global") == (0, 400)

    # Before expiry: nothing happens.
    assert await release(db, NOW + timedelta(seconds=TTL - 1)) == 0
    assert await amounts(db, "global") == (0, 400)

    # Expire the orphan only.
    async with db.transaction() as session:
        await session.execute(
            text("UPDATE budget_reservations SET expires_at = :e WHERE id = :i"),
            {"e": NOW, "i": orphan[0].reservation_id},
        )
    assert await release(db, NOW + timedelta(seconds=1)) == 1
    assert await amounts(db, "global") == (0, 300)
    assert await n_rows(db) == 1
    assert await release(db, NOW + timedelta(seconds=1)) == 0  # idempotent

    # Late settle of the orphan: charged, but reserved is not decremented again.
    await settle(db, orphan, 90)
    assert await amounts(db, "global") == (90, 300)
    await settle(db, live, 250)
    assert await amounts(db, "global") == (340, 0)


async def test_concurrent_release_never_double_decrements(db: Database) -> None:
    await add_counter(db, "global", 10_000)
    await reserve(db, "global", amount=100)
    later = NOW + timedelta(seconds=TTL + 1)
    first = db.sessionmaker()
    second = db.sessionmaker()
    try:
        async with first.begin():
            assert await release_expired(first, later) == 1  # rows locked, uncommitted
            async with second.begin():
                assert await release_expired(second, later) == 0  # skipped, not blocked
    finally:
        await first.close()
        await second.close()
    assert await amounts(db, "global") == (0, 0)
    both = await asyncio.gather(release(db, later), release(db, later))
    assert sum(both) == 0


async def test_free_reserve_settle_and_release(db: Database) -> None:
    day = NOW.date()

    async def free(tokens: int = 1000) -> Any:
        return await reserve_free(
            db,
            provider="openai",
            free_group="small",
            day=day,
            tokens=tokens,
            need=tokens,
            allowance=2500,
            ref="test",
            now=NOW,
            ttl_s=TTL,
        )

    async def used() -> int:
        async with db.session() as session:
            return int(
                (await session.execute(text("SELECT tokens FROM llm_free_usage"))).scalar_one()
            )

    hold = await free()
    assert hold is not None and await used() == 1000 and await n_rows(db, "free") == 1
    async with db.transaction() as session:
        await settle_free(session, hold, 400)
    assert await used() == 400 and await n_rows(db) == 0

    assert await free(2200) is None  # 400 + 2200 > 2500
    assert await used() == 400 and await n_rows(db) == 0

    orphan = await free(1000)
    assert orphan is not None and await used() == 1400
    assert await release(db, NOW + timedelta(seconds=TTL)) == 1
    assert await used() == 400
    async with db.transaction() as session:
        await settle_free(session, orphan, 300)  # late: the released tokens are charged now
    assert await used() == 700
    assert await n_rows(db) == 0


async def test_thresholds_published_once(db: Database) -> None:
    await add_counter(db, "global", 1000)
    for _ in range(2):
        holds = await reserve(db, "global", amount=100)
        await settle(db, holds, 600)  # 600 crosses 50 %; 1200 would exceed but is not capped here
    async with db.session() as session:
        rows = (
            await session.execute(
                text("SELECT payload->>'threshold_pct' FROM events WHERE topic = :t ORDER BY id"),
                {"t": "budget.threshold_reached"},
            )
        ).all()
    assert [r[0] for r in rows] == ["50", "80", "100"]


async def test_maintenance_runs_release(db: Database, settings: Settings) -> None:
    await add_counter(db, "global", 10_000)
    await reserve(db, "global", amount=100)
    async with db.transaction() as session:
        await session.execute(text("UPDATE budget_reservations SET expires_at = :e"), {"e": NOW})
    stop = asyncio.Event()
    task = asyncio.create_task(run_maintenance(db, settings, stop))
    try:
        async with asyncio.timeout(5):
            while await n_rows(db):
                await asyncio.sleep(0.05)
    finally:
        stop.set()
        await task
    assert await amounts(db, "global") == (0, 0)
    assert await run_singleton(db, "x", lambda s: release_expired(s, NOW)) == 0


async def test_router_leaves_no_reservation(db: Database, tmp_path: Path) -> None:
    (tmp_path / "check.md").write_text("You are a careful fact checker.\n")
    prompts = PromptRegistry([tmp_path])
    mid = await add_model(db, "openai", "paid-a")
    await add_route(db, mid)
    now = datetime.now(UTC)
    await add_counter(db, "global", 10_000_000, start=date(now.year, now.month, 1), end=_next(now))
    from tests.test_llm_router import ok, request

    router = LLMRouter(db, {"openai": FakeProvider(ok())}, prompts, reservation_ttl_s=60)
    result = await router.complete(request(level=Level.N1))
    assert isinstance(result.output, Answer)
    assert await n_rows(db) == 0


def _next(now: datetime) -> date:
    return date(now.year + (now.month == 12), now.month % 12 + 1, 1)


async def test_daily_periodic_tasks_run_at_startup(db: Database, settings: Settings) -> None:
    """Regression: last_run started at 0 on the monotonic clock (machine uptime), so a
    daily task never ran on a replica up for less than a day."""
    import asyncio

    from tower.core.jobs.maintenance import run_maintenance

    ran = asyncio.Event()

    async def daily(session: Any) -> int:
        ran.set()
        return 0

    stop = asyncio.Event()
    task = asyncio.create_task(run_maintenance(db, settings, stop, [("test:daily", 86400, daily)]))
    try:
        async with asyncio.timeout(5):
            await ran.wait()
    finally:
        stop.set()
        await task
