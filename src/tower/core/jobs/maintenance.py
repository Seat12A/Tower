"""Singleton periodic tasks (only one replica runs them, via advisory lock)."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain import error_codes as ec
from tower.core.errors import capture
from tower.core.jobs import queue as q
from tower.core.logging import get_logger

log = get_logger(__name__)

_TRY_LOCK = text("SELECT pg_try_advisory_xact_lock(hashtext(:name))")
_PURGE_RESOLVED_ERRORS = text(
    "DELETE FROM error_reports WHERE resolved_at < now() - make_interval(days => :days)"
)
_PURGE_EVENTS = text(
    """
    DELETE FROM events WHERE id IN (
        SELECT id FROM events WHERE created_at < now() - make_interval(days => :days)
        LIMIT 5000
    )
    """
)


async def run_singleton(
    db: Database, name: str, fn: Callable[[AsyncSession], Awaitable[int]]
) -> int | None:
    """Run ``fn`` unless another replica is doing it. None if the lock is taken."""
    async with db.transaction() as session:
        if not (await session.execute(_TRY_LOCK, {"name": name})).scalar_one():
            return None
        return await fn(session)


async def run_maintenance(
    db: Database,
    settings: Settings,
    stop: asyncio.Event,
    extra: Sequence[tuple[str, float, Callable[[AsyncSession], Awaitable[int]]]] = (),
) -> None:
    """Core tasks plus ``extra`` ones (periodic tasks of the enabled services)."""

    async def reap(session: AsyncSession) -> int:
        count = await q.reap(session)
        if count:
            capture(ec.JOB_LEASE_EXPIRED, message=f"{count} job(s) recovered", jobs=count)
        return count

    async def purge(session: AsyncSession) -> int:
        jobs = await q.purge(session, older_than_days=settings.job_retention_days)
        events = await session.execute(_PURGE_EVENTS, {"days": settings.event_retention_days})
        errors = await session.execute(
            _PURGE_RESOLVED_ERRORS, {"days": settings.error_retention_days}
        )
        return jobs + int(events.rowcount) + int(errors.rowcount)  # type: ignore[attr-defined]

    schedule: list[tuple[str, float, Callable[[AsyncSession], Awaitable[int]]]] = [
        ("tower:jobs:reap", 30.0, reap),
        ("tower:retention:purge", 3600.0, purge),
        *extra,
    ]
    last_run = dict.fromkeys((name for name, _, _ in schedule), 0.0)
    while not stop.is_set():
        now = time.monotonic()
        for name, every, fn in schedule:
            if now - last_run[name] < every:
                continue
            last_run[name] = now
            try:
                count = await run_singleton(db, name, fn)
                if count:
                    log.info("maintenance.done", task=name, rows=count)
            except Exception as exc:
                capture(ec.MNT_TASK_FAILED, exc, task=name)
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(10):
                await stop.wait()
