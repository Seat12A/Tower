"""Atlas legal jobs: the daily LEGI mirror synchronization."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.adapters.knowledge.legi_sync import sync_legi
from tower.core.config import Settings
from tower.core.domain import error_codes as ec
from tower.core.errors import capture
from tower.core.factcheck import names
from tower.core.jobs.queue import PermanentError, RetryLater, enqueue
from tower.core.services.base import JobContext

__all__ = ["enqueue_legi_sync", "legi_sync", "set_legi_overrides"]

RETRY_DELAY_S = 3600.0
MAX_ATTEMPTS = 3

_stream_factory: Callable[[str], AsyncIterator[bytes]] | None = None
_transport: Any = None


def set_legi_overrides(
    *, stream_factory: Callable[[str], AsyncIterator[bytes]] | None = None, transport: Any = None
) -> None:
    """Tests only: replace the archive stream factory and/or the httpx transport."""
    global _stream_factory, _transport
    _stream_factory = stream_factory
    _transport = transport


MAX_DIFFS_PER_RUN = 10
"""Daily diffs are ~6 MB each: ten fit well within the job timeout."""


async def enqueue_legi_sync(session: AsyncSession, settings: Settings) -> int:
    """Periodic (daily): one sync job per day (deduplicated). Returns 1 if enqueued."""
    today = datetime.now(UTC).date().isoformat()
    job_id = await enqueue(
        session,
        names.ATLAS_QUEUE,
        names.JOB_LEGI_SYNC,
        {},
        dedupe_key=names.legi_sync_key(today),
        max_attempts=MAX_ATTEMPTS,
    )
    return 0 if job_id is None else 1


async def legi_sync(ctx: JobContext) -> None:
    """Apply the new DILA archives (resumable: the state is saved after each archive)."""
    try:
        report = await sync_legi(
            ctx.db,
            ctx.settings,
            http_stream=_stream_factory,
            transport=_transport,
            diffs_only=True,
            max_archives=MAX_DIFFS_PER_RUN,
        )
    except Exception as exc:
        capture(ec.ATL_LEGI_SYNC_FAILED, exc, attempt=ctx.job.attempts)
        if ctx.job.attempts >= ctx.job.max_attempts:
            raise PermanentError("LEGI synchronization failed after the last attempt") from exc
        raise RetryLater(RETRY_DELAY_S, "LEGI synchronization failed") from exc
    if report.needs_bootstrap:
        ctx.log.warning("atlas.legi_needs_bootstrap")
        return
    s = report.stats
    ctx.log.info(
        "atlas.legi_synced",
        archives=len(report.archives),
        up_to_date=report.up_to_date,
        members=s.members,
        upserted=s.upserted,
        unchanged=s.unchanged,
        deleted=s.deleted,
        skipped=s.skipped,
        unsafe=s.unsafe,
        bytes=s.bytes,
        duration_s=report.duration_s,
    )
