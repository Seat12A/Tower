"""Exécution des jobs : concurrence bornée, bail, relances, arrêt gracieux."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

import structlog

from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain import error_codes as ec
from tower.core.errors import capture
from tower.core.events.channels import JOBS
from tower.core.events.listener import PgListener, Waker
from tower.core.jobs import queue as q
from tower.core.logging import get_logger
from tower.core.services.base import JobContext, JobSpec

log = get_logger(__name__)


class JobRunner:
    def __init__(
        self,
        db: Database,
        listener: PgListener,
        handlers: dict[str, JobSpec],
        queues: list[str],
        settings: Settings,
        *,
        worker_id: str | None = None,
    ) -> None:
        self.db = db
        self.listener = listener
        self.handlers = handlers
        self.queues = queues
        self.settings = settings
        self.worker_id = worker_id or settings.instance_id
        self.concurrency = settings.worker_concurrency
        self.lease = float(settings.job_lease_seconds)
        self._active: set[asyncio.Task[None]] = set()

    async def run(self, stop: asyncio.Event) -> None:
        if not self.queues:
            log.warning("runner.no_queues")
            await stop.wait()
            return
        log.info(
            "runner.started",
            queues=self.queues,
            concurrency=self.concurrency,
            worker=self.worker_id,
        )
        waker = self.listener.subscribe(JOBS)
        try:
            while not stop.is_set():
                free = self.concurrency - len(self._active)
                claimed = await self._claim(free) if free > 0 else 0
                if free > 0 and claimed == free:
                    continue  # la file en a peut-être d'autres : on recharge tout de suite
                await self._wait(waker, stop)
        finally:
            self.listener.unsubscribe(JOBS, waker)
            await self._drain()

    async def _claim(self, limit: int) -> int:
        try:
            async with self.db.transaction() as session:
                jobs = await q.claim(
                    session, self.queues, self.worker_id, limit=limit, lease=self.lease
                )
        except Exception as exc:
            capture(ec.DB_UNAVAILABLE, exc, message="Réservation de jobs impossible")
            await asyncio.sleep(self.settings.job_poll_interval)
            return 0
        for job in jobs:
            task = asyncio.create_task(self._execute(job), name=f"job-{job.id}")
            self._active.add(task)
            task.add_done_callback(self._active.discard)
        return len(jobs)

    async def _wait(self, waker: Waker, stop: asyncio.Event) -> None:
        wake = asyncio.create_task(waker.wait(self.settings.job_poll_interval))
        halt = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait({wake, halt, *self._active}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (wake, halt):
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t

    async def _drain(self) -> None:
        if not self._active:
            return
        log.info("runner.draining", active=len(self._active))
        _, pending = await asyncio.wait(
            set(self._active), timeout=self.settings.shutdown_grace_seconds
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _execute(self, job: q.JobRecord) -> None:
        jlog = log.bind(job_id=job.id, kind=job.kind, queue=job.queue, attempt=job.attempts)
        spec = self.handlers.get(job.kind)
        if spec is None:
            error = f"aucun handler pour {job.kind!r}"
            await self._finish(q.fail, job, error=f"{ec.JOB_UNKNOWN_KIND.code} {error}")
            capture(ec.JOB_UNKNOWN_KIND, message=error, **_job_ctx(job))
            return
        timeout = spec.timeout if spec.timeout is not None else self.lease * 0.9
        ctx = JobContext(job=job, db=self.db, settings=self.settings, log=jlog)
        started = time.perf_counter()
        structlog.contextvars.bind_contextvars(job_id=job.id)
        try:
            async with asyncio.timeout(timeout):
                await spec.handler(ctx)
        except q.RetryLater as exc:
            await self._finish(q.retry, job, delay=exc.delay, error=str(exc), refund_attempt=True)
            jlog.info("job.deferred", delay_s=exc.delay, reason=str(exc))
        except q.PermanentError as exc:
            await self._finish(q.fail, job, error=_err(exc, ec.JOB_PERMANENT))
            capture(ec.JOB_PERMANENT, exc, **_job_ctx(job))
        except asyncio.CancelledError:
            # Arrêt du processus : on rend le job à la file sans consommer de tentative.
            await self._finish(q.retry, job, delay=0, error="worker shutdown", refund_attempt=True)
            jlog.warning("job.released")
            raise
        except Exception as exc:
            if job.attempts >= job.max_attempts:
                code = ec.JOB_EXHAUSTED
                await self._finish(q.fail, job, error=_err(exc, code))
                capture(code, exc, **_job_ctx(job))
            else:
                code = ec.JOB_TIMEOUT if isinstance(exc, TimeoutError) else ec.JOB_ATTEMPT_FAILED
                delay = q.backoff_delay(job.attempts)
                await self._finish(q.retry, job, delay=delay, error=_err(exc, code))
                capture(code, exc, retry_in_s=round(delay, 1), **_job_ctx(job))
        else:
            ok = await self._finish(q.complete, job)
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            if ok:
                jlog.info("job.succeeded", duration_ms=duration_ms)
            else:
                jlog.warning("job.lease_lost", duration_ms=duration_ms)
        finally:
            structlog.contextvars.unbind_contextvars("job_id")

    async def _finish(self, op: object, job: q.JobRecord, **kwargs: object) -> bool:
        try:
            async with self.db.transaction() as session:
                return bool(await op(session, job.id, self.worker_id, **kwargs))  # type: ignore[operator]
        except Exception as exc:
            capture(ec.JOB_STATUS_UPDATE_FAILED, exc, **_job_ctx(job))
            return False  # le reaper rattrapera via l'expiration du bail


def _err(exc: BaseException, code: ec.ErrorCode) -> str:
    """Texte stocké dans ``jobs.last_error`` : code du catalogue en tête."""
    return f"{code.code} {type(exc).__name__}: {exc}"[:2000]


def _job_ctx(job: q.JobRecord) -> dict[str, Any]:
    return {
        "job_id": job.id,
        "job_kind": job.kind,
        "queue": job.queue,
        "attempt": job.attempts,
        "max_attempts": job.max_attempts,
    }
