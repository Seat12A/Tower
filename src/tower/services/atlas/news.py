"""News index polling: reference feeds → ``news_index`` (headline, lead, date, link only).

- ``enqueue_news_poll`` (periodic): one ``atlas.news_poll`` job per 15-minute slot, deduped by
  slot key, so a slow poll is never doubled.
- ``news_poll`` (job): active feeds polled with bounded concurrency, each one in isolation
  (a failing feed never fails the job). New items are inserted ``ON CONFLICT
  (canonical_url) DO NOTHING`` and clustered by SimHash (Hamming distance <= 3 on title +
  lead, last 72 hours): republications of one wire story share a ``cluster_id``.
- ``purge_news`` (periodic): deletes items older than the retention window, by batches.

The models never see the author or any editorial label of a feed (rule 10): feeds differ
only by their configuration.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.adapters.knowledge.atlas import canonical_url, registrable_domain
from tower.core.adapters.knowledge.rss import FeedEntry, FetchResult, UnsafeXml, fetch_feed
from tower.core.config import Settings
from tower.core.domain import error_codes as ec
from tower.core.domain.text import normalize, simhash64, simhash_bands
from tower.core.errors import capture
from tower.core.factcheck import names
from tower.core.jobs.queue import enqueue
from tower.core.logging import get_logger
from tower.core.models.atlas import NewsItem
from tower.core.ports.knowledge import SourceUnavailable

if TYPE_CHECKING:
    from tower.core.adapters.knowledge.http import SourceHttp
    from tower.core.services.base import JobContext

__all__ = [
    "SLOT_MINUTES",
    "enqueue_news_poll",
    "news_poll",
    "purge_news",
    "set_http_factory",
]

log = get_logger(__name__)

SLOT_MINUTES = 15
CLUSTER_WINDOW = timedelta(hours=72)
MAX_DISTANCE = 3
MIN_CLUSTER_WORDS = 6
"""Shorter texts are never clustered (too little signal; when in doubt, do not group)."""
PURGE_BATCH = 5_000
FAILURE_CAPTURE_EVERY = 10

HttpFactory = Callable[[Settings], "SourceHttp"]
_http_factory: HttpFactory | None = None


def set_http_factory(factory: HttpFactory | None) -> None:
    """Override how the HTTP client is built (tests inject a ``MockTransport``)."""
    global _http_factory
    _http_factory = factory


def _build_http(settings: Settings) -> SourceHttp:
    if _http_factory is not None:
        return _http_factory(settings)
    from tower.core.adapters.knowledge.http import SourceHttp

    return SourceHttp(
        "news",
        timeout=settings.atlas_http_timeout_s,
        user_agent=settings.http_user_agent,
        headers={"Accept": "application/rss+xml, application/atom+xml, application/xml, */*;q=0.5"},
    )


def _slot(now: datetime) -> str:
    floored = now.astimezone(UTC).replace(
        minute=now.minute - now.minute % SLOT_MINUTES, second=0, microsecond=0
    )
    return floored.strftime("%Y-%m-%dT%H:%M")


async def enqueue_news_poll(session: AsyncSession, settings: Settings) -> int:
    """Enqueue the poll of the current slot; 0 if a job of this slot is still active."""
    job_id = await enqueue(
        session,
        names.ATLAS_QUEUE,
        names.JOB_NEWS_POLL,
        {},
        dedupe_key=names.news_poll_key(_slot(datetime.now(UTC))),
        max_attempts=2,
    )
    return 0 if job_id is None else 1


_BAND_QUERY = """(SELECT a.id, a.cluster_id, a.published_at, a.simhash FROM news_index a
        WHERE a.simhash_b@ = :b@ AND a.published_at >= :since
          AND (a.published_at, a.id) < (:published, :id)
        ORDER BY a.published_at LIMIT 500)"""
_NEAR = text(
    "SELECT id, cluster_id FROM (\n"  # noqa: S608
    + "\nUNION\n".join(_BAND_QUERY.replace("@", str(i)) for i in range(4))
    + """
) c
    WHERE bit_count((c.simhash # :simhash)::bit(64)) <= :max_distance
    ORDER BY c.published_at, c.id
    LIMIT 1
    """
)
_SET_CLUSTER = text("UPDATE news_index SET cluster_id = :cluster WHERE id = :id")
_MARK_OK = text(
    """
    UPDATE news_feeds SET etag = :etag, last_modified = :last_modified, last_polled_at = :now,
        last_status = :status, failures = 0 WHERE id = :id
    """
)
_MARK_FAILED = text(
    """
    UPDATE news_feeds SET last_polled_at = :now, last_status = :status, failures = failures + 1
    WHERE id = :id RETURNING failures
    """
)
_ACTIVE = text(
    """
    SELECT id, url, publisher, country, lang, etag, last_modified FROM news_feeds
    WHERE active ORDER BY id
    """
)
_STATUS = re.compile(r"HTTP (\d{3})")


@dataclass(slots=True)
class _Fetched:
    feed: Any
    result: FetchResult | None = None
    error: Exception | None = None
    status: int | None = None


async def _fetch(http: SourceHttp, feed: Any, sem: asyncio.Semaphore, max_items: int) -> _Fetched:
    """Network phase of one feed (bounded concurrency); never raises."""
    fetched = _Fetched(feed)
    async with sem:
        try:
            fetched.result = await fetch_feed(
                http,
                feed,
                etag=feed.etag,
                last_modified=feed.last_modified,
                max_items=max_items,
            )
            fetched.status = fetched.result.status
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            fetched.error = exc
            if isinstance(exc, SourceUnavailable) and (m := _STATUS.search(str(exc))):
                fetched.status = int(m.group(1))
    return fetched


async def _store_entry(
    session: AsyncSession, feed: Any, entry: FeedEntry, published: datetime
) -> tuple[bool, bool]:
    """Insert one entry (skipped if its canonical URL exists) and cluster it.

    Returns (inserted, clustered into an older item).
    """
    url = canonical_url(entry.url)
    normalized = normalize(f"{entry.title} {entry.lead}")
    value = simhash64(normalized)
    bands = simhash_bands(value)
    row_id = (
        await session.execute(
            pg_insert(NewsItem)
            .values(
                feed_id=feed.id,
                origin="rss",
                url=entry.url,
                canonical_url=url,
                publisher=feed.publisher,
                domain=registrable_domain(url),
                country=feed.country,
                lang=feed.lang,
                title=entry.title,
                summary=entry.lead or None,
                published_at=published,
                simhash=value,
                simhash_b0=bands[0],
                simhash_b1=bands[1],
                simhash_b2=bands[2],
                simhash_b3=bands[3],
            )
            .on_conflict_do_nothing(index_elements=["canonical_url"])
            .returning(NewsItem.id)
        )
    ).scalar()
    if row_id is None:
        return False, False
    cluster = int(row_id)
    joined = False
    if len(normalized.split()) >= MIN_CLUSTER_WORDS:
        params: dict[str, Any] = {f"b{i}": band for i, band in enumerate(bands)}
        params.update(
            id=row_id,
            published=published,
            since=published - CLUSTER_WINDOW,
            simhash=value,
            max_distance=MAX_DISTANCE,
        )
        near = (await session.execute(_NEAR, params)).first()
        if near is not None:
            cluster = int(near.cluster_id if near.cluster_id is not None else near.id)
            joined = True
    await session.execute(_SET_CLUSTER, {"cluster": cluster, "id": row_id})
    return True, joined


async def _store_all(
    session: AsyncSession, fetched: list[_Fetched], now: datetime, cutoff: datetime
) -> tuple[int, int]:
    """Store the entries of every feed in publication order (so that the first publisher
    of a wire story is the cluster root whatever the feed order). Returns (new, clustered)."""
    pending: list[tuple[datetime, int, str, Any, FeedEntry]] = []
    for item in fetched:
        if item.result is None:
            continue
        for entry in item.result.entries:
            published = min(entry.published or now, now)
            if published >= cutoff:
                pending.append((published, item.feed.id, entry.url, item.feed, entry))
    pending.sort(key=lambda p: p[:3])
    new = clustered = 0
    broken: set[int] = set()
    for published, feed_id, _url, feed, entry in pending:
        try:
            async with session.begin_nested():  # one bad row never loses the batch
                inserted, joined = await _store_entry(session, feed, entry, published)
        except Exception as exc:
            if feed_id not in broken:
                broken.add(feed_id)
                capture(ec.ATL_FEED_FAILED, exc, feed_id=feed_id, stage="store")
            continue
        new += inserted
        clustered += joined
    return new, clustered


async def _mark(session: AsyncSession, item: _Fetched, now: datetime) -> bool:
    """Record the outcome of one feed; return True when it succeeded."""
    feed = item.feed
    if item.result is not None:
        await session.execute(
            _MARK_OK,
            {
                "id": feed.id,
                "etag": item.result.etag,
                "last_modified": item.result.last_modified,
                "now": now,
                "status": item.result.status,
            },
        )
        return True
    failures = int(
        (await session.execute(_MARK_FAILED, {"id": feed.id, "now": now, "status": item.status}))
        .one()
        .failures
    )
    if failures == 1 or failures % FAILURE_CAPTURE_EVERY == 0:
        unsafe = isinstance(item.error, UnsafeXml)
        capture(
            ec.ATL_UNSAFE_XML if unsafe else ec.ATL_FEED_FAILED,
            item.error,
            feed_id=feed.id,
            failures=failures,
            status=item.status,
        )
    return False


async def news_poll(ctx: JobContext) -> None:
    """Job ``atlas.news_poll``: poll every active feed once. Idempotent.

    Network phase: concurrent (semaphore). Storage phase: one transaction, serial, in
    publication order (concurrent writers would not see each other for clustering).
    """
    async with ctx.db.session() as session:
        feeds = (await session.execute(_ACTIVE)).all()
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=ctx.settings.atlas_news_retention_days)
    fetched: list[_Fetched] = []
    if feeds:
        sem = asyncio.Semaphore(ctx.settings.atlas_news_concurrency)
        http = _build_http(ctx.settings)
        try:
            fetched = list(
                await asyncio.gather(
                    *(_fetch(http, f, sem, ctx.settings.atlas_news_max_items) for f in feeds)
                )
            )
        finally:
            await http.aclose()
    async with ctx.db.transaction() as session:
        new, clustered = await _store_all(session, fetched, now, cutoff)
        ok = sum([await _mark(session, item, now) for item in fetched])
    log.info(
        "atlas.news_polled",
        feeds=len(feeds),
        ok=ok,
        failed=len(feeds) - ok,
        unsafe=sum(isinstance(i.error, UnsafeXml) for i in fetched),
        new_items=new,
        clustered=clustered,
    )


_PURGE = text(
    """
    DELETE FROM news_index WHERE id IN (
        SELECT id FROM news_index WHERE published_at < :cutoff LIMIT :batch
    )
    """
)


async def purge_news(session: AsyncSession, settings: Settings) -> int:
    """Delete items older than the retention window, 5 000 rows at a time."""
    cutoff = datetime.now(UTC) - timedelta(days=settings.atlas_news_retention_days)
    total = 0
    while True:
        result = await session.execute(_PURGE, {"cutoff": cutoff, "batch": PURGE_BATCH})
        deleted = int(result.rowcount or 0)  # type: ignore[attr-defined]
        total += deleted
        if deleted < PURGE_BATCH:
            return total
