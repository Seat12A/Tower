from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select, text

from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.events.listener import PgListener
from tower.core.jobs import queue as q
from tower.core.jobs.runner import JobRunner
from tower.core.models.errors import ErrorReport
from tower.core.models.jobs import Job
from tower.core.services.base import JobContext, JobSpec

from .conftest import make_settings


async def _job(db: Database, job_id: int) -> Job:
    async with db.session() as s:
        job = await s.get(Job, job_id)
        assert job is not None
        return job


async def _enqueue(db: Database, kind: str = "t.ok", **kw: Any) -> int:
    async with db.transaction() as s:
        job_id = await q.enqueue(s, "default", kind, kw.pop("payload", {}), **kw)
    assert job_id is not None
    return job_id


async def test_dedupe_key_only_among_active_jobs(db: Database) -> None:
    first = await _enqueue(db, dedupe_key="k")
    async with db.transaction() as s:
        assert await q.enqueue(s, "default", "t.ok", dedupe_key="k") is None
        await s.execute(text("UPDATE jobs SET status = 'succeeded' WHERE id = :i"), {"i": first})
    assert await _enqueue(db, dedupe_key="k") != first


async def test_concurrent_claims_never_overlap(db: Database) -> None:
    for _ in range(20):
        await _enqueue(db)

    async def claim(worker: str) -> list[int]:
        async with db.transaction() as s:
            return [j.id for j in await q.claim(s, ["default"], worker, limit=7, lease=30)]

    batches = await asyncio.gather(*(claim(f"w{i}") for i in range(4)))
    ids = [i for b in batches for i in b]
    assert len(ids) == len(set(ids)) == 20


async def test_priority_and_delay(db: Database) -> None:
    low = await _enqueue(db, priority=0)
    high = await _enqueue(db, priority=10)
    await _enqueue(db, delay=3600)
    async with db.transaction() as s:
        claimed = await q.claim(s, ["default"], "w", limit=10, lease=30)
    assert [j.id for j in claimed] == [high, low]


async def test_reap_expired_lease(db: Database) -> None:
    job_id = await _enqueue(db, max_attempts=2)
    async with db.transaction() as s:
        await q.claim(s, ["default"], "dead-worker", limit=1, lease=30)
        await s.execute(text("UPDATE jobs SET locked_until = now() - interval '1 second'"))
        assert await q.reap(s) == 1
    assert (await _job(db, job_id)).status == "queued"
    async with db.transaction() as s:
        await q.claim(s, ["default"], "dead-worker", limit=1, lease=30)
        await s.execute(text("UPDATE jobs SET locked_until = now() - interval '1 second'"))
        await q.reap(s)
    job = await _job(db, job_id)
    assert job.status == "failed"
    assert job.last_error is not None


async def _run_until(
    db: Database,
    listener: PgListener,
    handlers: dict[str, JobSpec],
    predicate: Any,
    settings: Settings | None = None,
    timeout: float = 10,
) -> None:
    runner = JobRunner(db, listener, handlers, ["default"], settings or make_settings())
    stop = asyncio.Event()
    task = asyncio.create_task(runner.run(stop))
    try:
        async with asyncio.timeout(timeout):
            while not await predicate():
                await asyncio.sleep(0.1)
    finally:
        stop.set()
        await task


async def test_runner_executes_and_completes(db: Database, listener: PgListener) -> None:
    done: list[int] = []

    async def ok(ctx: JobContext) -> None:
        done.append(ctx.job.payload["n"])

    ids = [await _enqueue(db, payload={"n": n}) for n in range(5)]

    async def finished() -> bool:
        statuses = [(await _job(db, i)).status for i in ids]
        return all(s == "succeeded" for s in statuses)

    await _run_until(db, listener, {"t.ok": JobSpec("t.ok", ok)}, finished)
    assert sorted(done) == list(range(5))


async def test_runner_retries_then_fails_and_stores_error(
    db: Database, listener: PgListener
) -> None:
    from tower.core.errors import get_reporter

    async def broken(_: JobContext) -> None:
        raise ValueError("source unavailable")

    job_id = await _enqueue(db, "t.broken", max_attempts=2)
    # Immediate retry for the test.
    original = q.backoff_delay
    q.backoff_delay = lambda attempt, **_: 0.0
    try:

        async def failed() -> bool:
            return (await _job(db, job_id)).status == "failed"

        await _run_until(db, listener, {"t.broken": JobSpec("t.broken", broken)}, failed)
    finally:
        q.backoff_delay = original

    job = await _job(db, job_id)
    assert job.attempts == 2
    assert job.last_error is not None and job.last_error.startswith("TWR-JOB-002 ValueError")
    await get_reporter().flush()
    async with db.session() as s:
        codes = {r.code: r.occurrences for r in await s.scalars(select(ErrorReport))}
    assert codes == {"TWR-JOB-006": 1, "TWR-JOB-002": 1}


async def test_retry_later_and_permanent_error(db: Database, listener: PgListener) -> None:
    calls = 0

    async def deferred(_: JobContext) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise q.RetryLater(0, "not yet")

    async def fatal(_: JobContext) -> None:
        raise q.PermanentError("post deleted")

    a = await _enqueue(db, "t.deferred")
    b = await _enqueue(db, "t.fatal")

    async def settled() -> bool:
        return (await _job(db, a)).status == "succeeded" and (await _job(db, b)).status == "failed"

    await _run_until(
        db,
        listener,
        {"t.deferred": JobSpec("t.deferred", deferred), "t.fatal": JobSpec("t.fatal", fatal)},
        settled,
    )
    assert (await _job(db, a)).attempts == 1  # RetryLater does not consume an attempt
    assert (await _job(db, b)).attempts == 1


async def test_unknown_kind_fails(db: Database, listener: PgListener) -> None:
    job_id = await _enqueue(db, "t.nobody")

    async def failed() -> bool:
        return (await _job(db, job_id)).status == "failed"

    await _run_until(db, listener, {}, failed)
    assert (await _job(db, job_id)).last_error.startswith("TWR-JOB-001")  # type: ignore[union-attr]


async def test_timeout_triggers_retry(db: Database, listener: PgListener) -> None:
    async def slow(_: JobContext) -> None:
        await asyncio.sleep(5)

    job_id = await _enqueue(db, "t.slow", max_attempts=1)

    async def failed() -> bool:
        return (await _job(db, job_id)).status == "failed"

    await _run_until(db, listener, {"t.slow": JobSpec("t.slow", slow, timeout=0.2)}, failed)


async def test_shutdown_releases_running_jobs(db: Database, listener: PgListener) -> None:
    started = asyncio.Event()

    async def forever(_: JobContext) -> None:
        started.set()
        await asyncio.sleep(60)

    job_id = await _enqueue(db, "t.forever")
    runner = JobRunner(
        db, listener, {"t.forever": JobSpec("t.forever", forever)}, ["default"], make_settings()
    )
    stop = asyncio.Event()
    task = asyncio.create_task(runner.run(stop))
    await asyncio.wait_for(started.wait(), 5)
    stop.set()
    await asyncio.wait_for(task, 5)
    job = await _job(db, job_id)
    assert job.status == "queued"
    assert job.attempts == 0  # attempt refunded
