"""Check of extension contents against the public embed (X oEmbed).

Bounded on purpose (docs/ingestion.md, "Vérification des contenus"):

- one batch job at a time, all replicas together (``dedupe_key``), enqueued every
  minute by a singleton periodic task; a batch makes at most ``oembed_per_minute``
  requests, ``oembed_concurrency`` at a time;
- at most ``oembed_daily_max`` requests per UTC day (counted on ``checked_at``);
- only contents first seen within ``oembed_max_age_h``, each checked once;
- a 429 or a refusal pauses the whole check (the batch job is postponed and, being
  active, blocks the next ones).

No transaction is open during the HTTP requests. Watched authors come first (their
posts may open a targeted fact-check), then the most recent contents.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.config import Settings
from tower.core.domain import error_codes as ec
from tower.core.domain.security import Actor
from tower.core.domain.verification import Verification, compare
from tower.core.errors import capture
from tower.core.jobs.queue import RetryLater, enqueue
from tower.core.logging import get_logger
from tower.core.services.base import JobContext

if TYPE_CHECKING:
    from tower.core.ports.platforms import PublicEmbedClient

log = get_logger(__name__)

QUEUE = "ingest"
JOB_VERIFY = "ingest.verify"
DEDUPE_KEY = "oembed:batch"
PRIORITY = 10
"""Ahead of the ``ingest.link_copies`` backlog of a wave (same queue)."""
BATCH_DEADLINE_S = 80.0
"""No request starts after this (job timeout 110 s): leftovers go to the next batch."""
PAUSE_ON_FAILURE_S = 300.0
"""Pause after a batch where every request failed (X unreachable or refusing us)."""

_ACTOR = Actor.system("ingest")
_client: PublicEmbedClient | None = None
_override: PublicEmbedClient | None = None

_COUNT_TODAY = text("SELECT count(*) FROM content_items WHERE checked_at >= :since")

_CANDIDATES = text(
    """
    (SELECT c.id, c.text, a.handle, 0 AS rank, c.first_seen_at
       FROM content_items c
       JOIN watched_accounts w ON w.account_id = c.author_account_id AND w.active
       LEFT JOIN platform_accounts a ON a.id = c.author_account_id
      WHERE c.verification = 'unverified' AND c.checked_at IS NULL
        AND c.platform = 'x' AND c.first_seen_at >= :since
      ORDER BY c.first_seen_at DESC
      LIMIT :n)
    UNION ALL
    (SELECT c.id, c.text, a.handle, 1 AS rank, c.first_seen_at
       FROM content_items c
       LEFT JOIN platform_accounts a ON a.id = c.author_account_id
      WHERE c.verification = 'unverified' AND c.checked_at IS NULL
        AND c.platform = 'x' AND c.first_seen_at >= :since
      ORDER BY c.first_seen_at DESC
      LIMIT :n)
    ORDER BY rank, first_seen_at DESC
    """
)

_EXTERNAL_ID = text("SELECT external_id FROM content_items WHERE id = :id")


def set_client(client: PublicEmbedClient | None) -> None:
    """Replace the embed client (tests); ``None`` restores the real one."""
    global _override
    _override = client


def get_client(settings: Settings) -> PublicEmbedClient:
    if _override is not None:
        return _override
    global _client
    if _client is None:
        from tower.core.adapters.x_oembed import XOEmbedClient

        _client = XOEmbedClient(url=settings.oembed_url, timeout=settings.oembed_timeout_s)
    return _client


async def schedule_verification(session: AsyncSession, settings: Settings) -> int:
    """Periodic (every minute): enqueue the batch job unless one is already active."""
    if not settings.oembed_enabled or settings.oembed_daily_max == 0:
        return 0
    job_id = await enqueue(session, QUEUE, JOB_VERIFY, {}, priority=PRIORITY, dedupe_key=DEDUPE_KEY)
    return 0 if job_id is None else 1


async def _batch(ctx: JobContext, now: datetime) -> list[dict[str, Any]]:
    settings = ctx.settings
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    async with ctx.db.transaction() as session:
        done_today = int((await session.execute(_COUNT_TODAY, {"since": midnight})).scalar_one())
        budget = min(settings.oembed_per_minute, settings.oembed_daily_max - done_today)
        if budget <= 0:
            return []
        since = now - timedelta(hours=settings.oembed_max_age_h)
        rows = (await session.execute(_CANDIDATES, {"since": since, "n": budget})).all()
        picked: list[dict[str, Any]] = []
        seen: set[int] = set()
        for row in rows:
            if row.id in seen:
                continue
            seen.add(row.id)
            external_id = (await session.execute(_EXTERNAL_ID, {"id": row.id})).scalar_one()
            picked.append(
                {"id": row.id, "external_id": external_id, "text": row.text, "handle": row.handle}
            )
            if len(picked) >= budget:
                break
        return picked


async def verify_batch(ctx: JobContext) -> None:
    """Batch job: check up to ``oembed_per_minute`` contents, then stop."""
    from tower.core.ports.platforms import PlatformError, PlatformRateLimited
    from tower.core.usecases.ingestion import record_verification

    settings = ctx.settings
    if not settings.oembed_enabled:
        return
    now = datetime.now(UTC)
    batch = await _batch(ctx, now)
    if not batch:
        return
    client = get_client(settings)
    semaphore = asyncio.Semaphore(settings.oembed_concurrency)
    paused: list[float] = []
    counts = {v.value: 0 for v in Verification}
    failures = 0
    deadline = asyncio.get_running_loop().time() + BATCH_DEADLINE_S

    async def check(item: dict[str, Any]) -> None:
        nonlocal failures
        if paused:
            return
        async with semaphore:
            if paused or asyncio.get_running_loop().time() > deadline:
                return
            try:
                embed = await client.lookup(item["external_id"])
            except PlatformRateLimited as exc:
                paused.append(exc.retry_after)
                return
            except PlatformError as exc:
                failures += 1
                capture(ec.ING_EMBED_UNAVAILABLE, exc, content=item["id"])
                return
        outcome = compare(item["text"], item["handle"], embed)
        async with ctx.db.transaction() as session:
            if await record_verification(session, _ACTOR, item["id"], outcome):
                counts[outcome.value] += 1
        if outcome is Verification.MISMATCH:
            capture(
                ec.ING_CONTENT_MISMATCH,
                message="Extension content contradicts the public embed",
                content=item["id"],
            )

    await asyncio.gather(*(check(item) for item in batch))
    log.info(
        "ingest.verify.batch",
        requested=len(batch),
        failures=failures,
        **{k: v for k, v in counts.items() if v},
    )
    if paused:
        capture(
            ec.ING_EMBED_RATE_LIMITED,
            message="Public embed rate limited: verification paused",
            retry_after_s=round(paused[0]),
        )
        raise RetryLater(paused[0], "public embed rate limited")
    if failures and failures == len(batch):
        raise RetryLater(PAUSE_ON_FAILURE_S, "public embed unavailable")
