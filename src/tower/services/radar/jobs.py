"""Radar jobs: read a watched account, resolve a handle, react to watchlist changes."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, NoReturn

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.budget import BudgetHold, BudgetRefused, reserve_budget, settle_budget
from tower.core.db.database import Database
from tower.core.domain import error_codes as ec
from tower.core.domain.errors import Conflict
from tower.core.domain.ingestion import AuthorInput, ItemInput, MediaInput, as_utc
from tower.core.domain.radar import next_interval
from tower.core.domain.security import Actor
from tower.core.errors import capture
from tower.core.events.outbox import EventRecord
from tower.core.jobs.queue import PermanentError, RetryLater, enqueue
from tower.core.logging import get_logger
from tower.core.ports.platforms import (
    ContentItem,
    PlatformAuthFailed,
    PlatformBadResponse,
    PlatformError,
    PlatformNotConfigured,
    PlatformRateLimited,
    PlatformUnavailable,
    PollingPlatformClient,
    PollPage,
    ReadUsage,
)
from tower.core.services.base import JobContext
from tower.core.usecases.ingestion import ingest_items
from tower.core.usecases.watchlist import resolve_watched_account
from tower.services.radar.client import SUPPORTED_PLATFORMS, get_client
from tower.services.radar.schedule import QUEUE, bounds_from_settings

log = get_logger(__name__)

ACTOR = "system:radar"
_MIN_RETRY_S = 60

_LOAD = text(
    """
    SELECT w.id, w.priority, w.since_cursor, w.consecutive_empty, w.active,
           a.platform, a.external_id, a.handle
    FROM watched_accounts w JOIN platform_accounts a ON a.id = w.account_id
    WHERE w.public_id = :public_id
    """
)
# Scheduling columns: plain SQL, never the ORM (``version_id_col`` must stay untouched).
_UPDATE_SCHEDULE = text(
    """
    UPDATE watched_accounts SET
        since_cursor = coalesce(CAST(:since AS text), since_cursor),
        last_polled_at = CAST(:now AS timestamptz),
        last_post_at = coalesce(CAST(:last_post AS timestamptz), last_post_at),
        consecutive_empty = :empty,
        poll_interval_s = CAST(:interval AS integer),
        next_poll_at = CAST(:now AS timestamptz) + make_interval(secs => CAST(:interval AS integer))
    WHERE id = :id
    """
)
_PUSH_POLL = text(
    """
    UPDATE watched_accounts
    SET next_poll_at = CAST(:now AS timestamptz) + make_interval(secs => :secs)
    WHERE id = :id
    """
)


class _Watched:
    __slots__ = (
        "active",
        "consecutive_empty",
        "external_id",
        "handle",
        "id",
        "platform",
        "priority",
        "public_id",
        "since_cursor",
    )

    def __init__(self, public_id: uuid.UUID, row: Any) -> None:
        self.public_id = public_id
        self.id = int(row[0])
        self.priority = int(row[1])
        self.since_cursor: str | None = row[2]
        self.consecutive_empty = int(row[3])
        self.active = bool(row[4])
        self.platform: str = row[5]
        self.external_id: str | None = row[6]
        self.handle: str | None = row[7]


def to_item_input(item: ContentItem) -> ItemInput:
    """Generic conversion of a platform item to the ingestion input."""
    author = None
    if item.author_external_id is not None or item.author_handle is not None:
        author = AuthorInput(external_id=item.author_external_id, handle=item.author_handle)
    return ItemInput(
        platform=item.platform,
        external_id=item.external_id,
        text=item.text,
        author=author,
        lang_hint=item.lang_hint,
        created_at=item.created_at,
        metrics=dict(item.metrics.values) if item.metrics is not None else {},
        parent_external_id=item.parent_external_id,
        quoted_external_id=item.quoted_external_id,
        media=tuple(MediaInput(kind=m.kind, url=m.url) for m in item.media),
    )


async def _load(db: Database, raw_id: object) -> _Watched | None:
    try:
        public_id = uuid.UUID(str(raw_id))
    except ValueError:
        log.warning("radar.bad_payload")
        return None
    async with db.session() as session:
        row = (await session.execute(_LOAD, {"public_id": public_id})).first()
    if row is None:
        log.info("radar.watched_missing", watched=str(public_id))
        return None
    return _Watched(public_id, row)


def _usable(w: _Watched) -> bool:
    return w.active and w.external_id is not None and w.platform in SUPPORTED_PLATFORMS


async def _settle(db: Database, settled: Sequence[tuple[tuple[BudgetHold, ...], int]]) -> None:
    """Settle holds at their real cost (own transaction)."""
    if not settled:
        return
    async with db.transaction() as session:
        for holds, cost in settled:
            await settle_budget(session, holds, cost, actor=ACTOR)


def _cost(client: PollingPlatformClient, usage: ReadUsage) -> int:
    return (
        usage.posts * client.cost_per_read_usd_micros
        + usage.users * client.cost_per_account_read_usd_micros
    )


async def _handle_platform_error(
    db: Database, exc: PlatformError, w: _Watched, ctx: JobContext, *, now: datetime
) -> None:
    """Translate a platform error (holds already settled) into capture + job outcome."""
    context = {"watched": str(w.public_id), "platform": w.platform}
    if isinstance(exc, PlatformRateLimited):
        raise RetryLater(exc.retry_after, "platform rate limited") from exc
    if isinstance(exc, PlatformNotConfigured):
        capture(ec.RAD_NOT_CONFIGURED, exc, **context)
        raise PermanentError("platform not configured") from exc
    if isinstance(exc, PlatformAuthFailed):
        capture(ec.RAD_AUTH_FAILED, exc, **context)
        raise PermanentError("platform refused the credentials") from exc
    if isinstance(exc, PlatformBadResponse) and exc.status == 404:
        capture(ec.RAD_ACCOUNT_NOT_FOUND, exc, **context)
        async with db.transaction() as session:
            await session.execute(
                _PUSH_POLL,
                {"id": w.id, "now": now, "secs": ctx.settings.radar_interval_max_s},
            )
        return
    if isinstance(exc, PlatformUnavailable):
        capture(ec.RAD_UNAVAILABLE, exc, **context)
        raise exc
    capture(exc.code, exc, **context)
    raise PermanentError("unexpected platform response") from exc


def _no_client(exc: PlatformNotConfigured, w: _Watched) -> NoReturn:
    capture(ec.RAD_NOT_CONFIGURED, exc, watched=str(w.public_id), platform=w.platform)
    raise PermanentError("platform not configured") from exc


async def poll_account(ctx: JobContext) -> None:
    db, settings = ctx.db, ctx.settings
    w = await _load(db, ctx.job.payload.get("watched"))
    if w is None:
        return
    if not _usable(w) or w.external_id is None:
        log.info("radar.poll_skipped", watched=str(w.public_id), platform=w.platform)
        return
    try:
        client = get_client(settings, w.platform)
    except PlatformNotConfigured as exc:
        _no_client(exc, w)
    scope = f"provider:{w.platform}"
    ref = f"radar:poll:{w.public_id}"
    now = datetime.now(UTC)
    first_read = w.since_cursor is None
    max_pages = 1 if first_read else settings.radar_max_pages
    estimate = settings.radar_max_results * client.cost_per_read_usd_micros

    done: list[tuple[tuple[BudgetHold, ...], PollPage]] = []
    token: str | None = None
    for _ in range(max_pages):
        try:
            holds = await reserve_budget(
                db, (scope,), estimate, ref=ref, now=now, ttl_s=settings.budget_reservation_ttl_s
            )
        except BudgetRefused as exc:
            await _settle(db, [(h, _cost(client, p.usage)) for h, p in done])
            log.warning("radar.budget_exhausted", watched=str(w.public_id), scope=exc.scope)
            wait = (exc.retry_at - datetime.now(UTC)).total_seconds()
            raise RetryLater(max(_MIN_RETRY_S, wait), "budget exhausted") from exc
        try:
            page = await client.poll_page(
                w.external_id,
                since=w.since_cursor,
                max_results=settings.radar_max_results,
                pagination_token=token,
                exclude=settings.radar_exclude_list,
            )
        except PlatformError as exc:
            await _settle(db, [*((h, _cost(client, p.usage)) for h, p in done), (holds, 0)])
            await _handle_platform_error(db, exc, w, ctx, now=now)
            return
        done.append((holds, page))
        token = page.next_token
        if token is None:
            break

    items = [i for _, page in done for i in page.items]
    result_new = 0
    async with db.transaction() as session:
        for holds, page in done:
            await settle_budget(session, holds, _cost(client, page.usage), actor=ACTOR)
        if items:
            result = await ingest_items(
                session,
                Actor.system("radar"),
                [to_item_input(i) for i in items],
                source="radar",
                filter_version=None,
                installation_token=None,
                max_items=len(items),
                daily_quota=len(items),
                now=now,
            )
            result_new = result.new
        await _record_poll(session, w, done, items, now=now, settings=settings)
    log.info(
        "radar.polled",
        watched=str(w.public_id),
        items=len(items),
        new=result_new,
        cost_usd_micros=sum(_cost(client, p.usage) for _, p in done),
    )


async def _record_poll(
    session: AsyncSession,
    w: _Watched,
    done: list[tuple[tuple[BudgetHold, ...], PollPage]],
    items: list[ContentItem],
    *,
    now: datetime,
    settings: Any,
) -> None:
    empty = 0 if items else w.consecutive_empty + 1
    interval = next_interval(
        priority=w.priority,
        new_items=len(items),
        consecutive_empty=empty,
        bounds=bounds_from_settings(settings),
    )
    created = [as_utc(i.created_at) for i in items if i.created_at is not None]
    await session.execute(
        _UPDATE_SCHEDULE,
        {
            "id": w.id,
            "since": done[0][1].newest_id if items and done else None,
            "now": now,
            "last_post": max(created) if created else None,
            "empty": empty,
            "interval": interval,
        },
    )


async def resolve_account(ctx: JobContext) -> None:
    db, settings = ctx.db, ctx.settings
    w = await _load(db, ctx.job.payload.get("watched"))
    if w is None:
        return
    if w.external_id is not None or not w.active or w.handle is None:
        log.info("radar.resolve_skipped", watched=str(w.public_id))
        return
    try:
        client = get_client(settings, w.platform)
    except PlatformNotConfigured as exc:
        _no_client(exc, w)
    now = datetime.now(UTC)
    holds = await _reserve_resolve(ctx, client, w, now)
    try:
        account = await client.get_account(w.handle)
    except PlatformError as exc:
        await _settle(db, [(holds, 0)])
        await _handle_platform_error(db, exc, w, ctx, now=now)
        return
    async with db.transaction() as session:
        await settle_budget(
            session,
            holds,
            client.cost_per_account_read_usd_micros if account is not None else 0,
            actor=ACTOR,
        )
        if account is None:
            capture(
                ec.RAD_ACCOUNT_NOT_FOUND,
                message="Handle not found on the platform",
                watched=str(w.public_id),
                handle=w.handle,
            )
            return
        try:
            async with session.begin_nested():
                await resolve_watched_account(
                    session,
                    Actor.system("radar"),
                    watched_public_id=str(w.public_id),
                    external_id=account.external_id,
                    handle=account.handle,
                )
        except Conflict as exc:
            log.warning("radar.resolve_conflict", watched=str(w.public_id), error=str(exc))


async def _reserve_resolve(
    ctx: JobContext, client: PollingPlatformClient, w: _Watched, now: datetime
) -> tuple[BudgetHold, ...]:
    try:
        return await reserve_budget(
            ctx.db,
            (f"provider:{w.platform}",),
            client.cost_per_account_read_usd_micros,
            ref=f"radar:resolve:{w.public_id}",
            now=now,
            ttl_s=ctx.settings.budget_reservation_ttl_s,
        )
    except BudgetRefused as exc:
        log.warning("radar.budget_exhausted", watched=str(w.public_id), scope=exc.scope)
        wait = (exc.retry_at - datetime.now(UTC)).total_seconds()
        raise RetryLater(max(_MIN_RETRY_S, wait), "budget exhausted") from exc


async def on_watchlist_changed(session: AsyncSession, events: Sequence[EventRecord]) -> None:
    """Short subscription handler: enqueue the resolution of pending handles."""
    for event in events:
        payload = event.payload
        watched = payload.get("watched_account")
        if payload.get("action") not in ("added", "updated") or not isinstance(watched, dict):
            continue
        if watched.get("external_id") is not None or not watched.get("active"):
            continue
        public_id = watched.get("public_id")
        if not public_id:
            continue
        await enqueue(
            session,
            QUEUE,
            "radar.resolve_account",
            {"watched": str(public_id)},
            dedupe_key=f"radar:resolve:{public_id}",
        )
