"""PostgreSQL job queue.

- ``enqueue``: inside the caller's transaction (the job exists only if it commits),
  with optional deduplication on ``dedupe_key`` among active jobs.
- ``claim``: reserves a batch via ``FOR UPDATE SKIP LOCKED`` and sets a lease
  (``locked_until``). As many workers as needed, without double execution.
- An expired lease (worker killed, OOM, redeployment) is recovered by ``reap``.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import orjson
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.events.channels import JOBS


class RetryLater(Exception):
    """Raise in a handler to reschedule the job (without logging an error)."""

    def __init__(self, delay: float, reason: str = "retry requested") -> None:
        super().__init__(reason)
        self.delay = delay


class PermanentError(Exception):
    """Raise to mark the job as permanently failed, with no further retry."""


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: int
    queue: str
    kind: str
    payload: dict[str, Any]
    priority: int
    attempts: int
    max_attempts: int
    dedupe_key: str | None
    created_at: datetime


_ENQUEUE = text(
    """
    WITH ins AS (
        INSERT INTO jobs (queue, kind, payload, priority, run_at, dedupe_key, max_attempts)
        VALUES (:queue, :kind, CAST(:payload AS jsonb), :priority,
                now() + make_interval(secs => :delay), :dedupe_key, :max_attempts)
        ON CONFLICT (dedupe_key) WHERE status IN ('queued', 'running') DO NOTHING
        RETURNING id
    )
    SELECT id, pg_notify(:channel, :queue) FROM ins
    """
)

_CLAIM = text(
    """
    WITH picked AS (
        SELECT id FROM jobs
        WHERE status = 'queued' AND queue = ANY(:queues) AND run_at <= now()
        ORDER BY priority DESC, run_at, id
        LIMIT :limit
        FOR UPDATE SKIP LOCKED
    )
    UPDATE jobs AS j
    SET status = 'running', attempts = j.attempts + 1, locked_by = :worker,
        locked_until = now() + make_interval(secs => :lease), started_at = now()
    FROM picked WHERE j.id = picked.id
    RETURNING j.id, j.queue, j.kind, j.payload, j.priority, j.attempts, j.max_attempts,
              j.dedupe_key, j.created_at
    """
)

_COMPLETE = text(
    """
    UPDATE jobs SET status = 'succeeded', finished_at = now(), locked_by = NULL,
                    locked_until = NULL, last_error = NULL
    WHERE id = :id AND status = 'running' AND locked_by = :worker
    """
)

_RESCHEDULE = text(
    """
    UPDATE jobs SET status = 'queued', run_at = now() + make_interval(secs => :delay),
                    locked_by = NULL, locked_until = NULL, last_error = :error,
                    attempts = attempts - CASE WHEN :refund THEN 1 ELSE 0 END
    WHERE id = :id AND status = 'running' AND locked_by = :worker
    """
)

_FAIL = text(
    """
    UPDATE jobs SET status = 'failed', finished_at = now(), locked_by = NULL,
                    locked_until = NULL, last_error = :error
    WHERE id = :id AND status = 'running' AND locked_by = :worker
    """
)

_REAP = text(
    """
    UPDATE jobs SET
        status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'queued' END,
        finished_at = CASE WHEN attempts >= max_attempts THEN now() END,
        run_at = now(), locked_by = NULL, locked_until = NULL,
        last_error = 'lease expired (worker lost)'
    WHERE status = 'running' AND locked_until < now()
    """
)

_PURGE = text(
    """
    DELETE FROM jobs WHERE id IN (
        SELECT id FROM jobs
        WHERE status IN ('succeeded', 'failed', 'cancelled')
          AND finished_at < now() - make_interval(days => :days)
        LIMIT :batch
    )
    """
)


async def enqueue(
    session: AsyncSession,
    queue: str,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    priority: int = 0,
    delay: float | timedelta = 0,
    dedupe_key: str | None = None,
    max_attempts: int = 5,
) -> int | None:
    """Add a job. Return its id, or None if an active job has the same ``dedupe_key``."""
    seconds = delay.total_seconds() if isinstance(delay, timedelta) else float(delay)
    result = await session.execute(
        _ENQUEUE,
        {
            "queue": queue,
            "kind": kind,
            "payload": orjson.dumps(payload or {}).decode(),
            "priority": priority,
            "delay": seconds,
            "dedupe_key": dedupe_key,
            "max_attempts": max_attempts,
            "channel": JOBS,
        },
    )
    job_id = result.scalar_one_or_none()
    return None if job_id is None else int(job_id)


async def claim(
    session: AsyncSession, queues: list[str], worker: str, *, limit: int, lease: float
) -> list[JobRecord]:
    rows = await session.execute(
        _CLAIM, {"queues": queues, "worker": worker, "limit": limit, "lease": lease}
    )
    return [JobRecord(**row._mapping) for row in rows]


async def complete(session: AsyncSession, job_id: int, worker: str) -> bool:
    result = await session.execute(_COMPLETE, {"id": job_id, "worker": worker})
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def retry(
    session: AsyncSession,
    job_id: int,
    worker: str,
    *,
    delay: float,
    error: str | None,
    refund_attempt: bool = False,
) -> bool:
    result = await session.execute(
        _RESCHEDULE,
        {"id": job_id, "worker": worker, "delay": delay, "error": error, "refund": refund_attempt},
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def fail(session: AsyncSession, job_id: int, worker: str, *, error: str) -> bool:
    result = await session.execute(_FAIL, {"id": job_id, "worker": worker, "error": error})
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def reap(session: AsyncSession) -> int:
    """Requeue (or fail) jobs whose lease has expired."""
    result = await session.execute(_REAP)
    return int(result.rowcount)  # type: ignore[attr-defined]


async def purge(session: AsyncSession, *, older_than_days: int, batch: int = 1000) -> int:
    result = await session.execute(_PURGE, {"days": older_than_days, "batch": batch})
    return int(result.rowcount)  # type: ignore[attr-defined]


def backoff_delay(attempt: int, *, base: float = 5.0, cap: float = 3600.0) -> float:
    """Capped exponential backoff, with jitter (avoids synchronized retries)."""
    exp = min(cap, base * float(2 ** max(attempt - 1, 0)))
    return exp / 2 + random.uniform(0, exp / 2)  # noqa: S311
