from __future__ import annotations

import subprocess
import sys
import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import structlog
from sqlalchemy import text

from tower.core.budget import release_expired
from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain.security import Actor
from tower.core.errors import get_reporter
from tower.core.events.outbox import Cursor, read_events
from tower.core.jobs.maintenance import run_singleton
from tower.core.jobs.queue import JobRecord, PermanentError, RetryLater, enqueue
from tower.core.ports.platforms import (
    ContentItem,
    MediaRef,
    Metrics,
    PlatformAuthFailed,
    PlatformBadResponse,
    PlatformNotConfigured,
    PlatformRateLimited,
    PlatformUnavailable,
    PollPage,
    PublicAccount,
    ReadUsage,
    ThreadContext,
    UnsupportedOperation,
)
from tower.core.services.base import JobContext
from tower.core.services.registry import load_services
from tower.core.usecases import watchlist as uc
from tower.services.radar import client as client_mod
from tower.services.radar import service
from tower.services.radar.client import get_client, set_client_override
from tower.services.radar.jobs import (
    on_watchlist_changed,
    poll_account,
    resolve_account,
    to_item_input,
)
from tower.services.radar.schedule import schedule_polls

from .conftest import make_settings

EDITOR = Actor.system("test")
NOW = datetime.now(UTC)


class FakeClient:
    platform = "x"
    cost_per_read_usd_micros = 5_000
    cost_per_account_read_usd_micros = 10_000

    def __init__(
        self,
        pages: Sequence[PollPage] = (),
        *,
        error: Exception | None = None,
        account: PublicAccount | None = None,
    ) -> None:
        self.pages = list(pages)
        self.error = error
        self.account = account
        self.calls: list[dict[str, Any]] = []
        self.account_calls: list[str] = []

    async def poll_page(
        self,
        account_ref: str,
        *,
        since: str | None,
        max_results: int,
        pagination_token: str | None = None,
        exclude: Sequence[str] = (),
    ) -> PollPage:
        self.calls.append(
            {
                "ref": account_ref,
                "since": since,
                "max_results": max_results,
                "token": pagination_token,
                "exclude": list(exclude),
            }
        )
        if self.error is not None:
            raise self.error
        return self.pages.pop(0) if self.pages else _page([])

    async def get_account(self, account_ref: str) -> PublicAccount | None:
        self.account_calls.append(account_ref)
        if self.error is not None:
            raise self.error
        return self.account

    async def get_item(self, external_id: str) -> ContentItem | None:
        raise UnsupportedOperation

    async def get_thread_context(self, item: ContentItem) -> ThreadContext:
        raise UnsupportedOperation

    async def get_metrics(self, external_id: str) -> Metrics | None:
        raise UnsupportedOperation

    async def poll_account(self, account_ref: str, *, since: str | None) -> Sequence[ContentItem]:
        raise UnsupportedOperation


def _item(ext: str, text_: str | None = None, **kw: Any) -> ContentItem:
    return ContentItem(
        platform="x",
        external_id=ext,
        text=text_ or f"Unique statement number {ext} about public spending",
        author_external_id="5000",
        author_handle="someone",
        created_at=NOW - timedelta(minutes=5),
        **kw,
    )


def _page(
    items: Sequence[ContentItem], next_token: str | None = None, posts: int | None = None
) -> PollPage:
    return PollPage(
        items=tuple(items),
        newest_id=items[0].external_id if items else None,
        next_token=next_token,
        usage=ReadUsage(posts=len(items) if posts is None else posts),
    )


@pytest.fixture(autouse=True)
def _reset_clients() -> Iterator[None]:
    yield
    set_client_override("x", None)
    client_mod._clients.clear()


def _ctx(db: Database, settings: Settings, kind: str, payload: dict[str, Any]) -> JobContext:
    job = JobRecord(
        id=1,
        queue="radar",
        kind=kind,
        payload=payload,
        priority=0,
        attempts=1,
        max_attempts=3,
        dedupe_key=None,
        created_at=NOW,
    )
    return JobContext(job=job, db=db, settings=settings, log=structlog.get_logger("test"))


async def _one(db: Database, sql: str, **params: Any) -> Any:
    async with db.session() as s:
        return (await s.execute(text(sql), params)).scalar_one()


async def _watch(
    db: Database,
    ext: str | None = "5000",
    *,
    priority: int = 0,
    handle: str | None = None,
    next_poll_at: datetime | None = None,
    active: bool = True,
    since: str | None = None,
) -> str:
    async with db.transaction() as s:
        w, _ = await uc.add_watched_account(
            s,
            EDITOR,
            platform="x",
            external_id=ext,
            handle=handle,
            category="media_outlet",
            priority=priority,
        )
        await s.execute(
            text(
                "UPDATE watched_accounts SET next_poll_at = :n, active = :a, since_cursor = :c "
                "WHERE id = :i"
            ),
            {"n": next_poll_at, "a": active, "c": since, "i": w.id},
        )
        return str(w.public_id)


async def _set_cap(db: Database, cap: int, spent: int = 0) -> None:
    async with db.transaction() as s:
        await s.execute(
            text(
                "INSERT INTO budget_counters (scope, period_start, period_end, cap_usd_micros, "
                "spent_usd_micros) VALUES ('provider:x', date_trunc('month', now())::date, "
                "(date_trunc('month', now()) + interval '1 month')::date, :c, :s)"
            ),
            {"c": cap, "s": spent},
        )


async def _counter(db: Database) -> tuple[int, int, int]:
    async with db.session() as s:
        row = (
            await s.execute(
                text(
                    "SELECT cap_usd_micros, spent_usd_micros, reserved_usd_micros "
                    "FROM budget_counters WHERE scope = 'provider:x'"
                )
            )
        ).one()
    return int(row[0]), int(row[1]), int(row[2])


async def _schedule(db: Database, settings: Settings) -> int:
    async with db.transaction() as s:
        return await schedule_polls(s, settings)


async def _queued(db: Database) -> list[str]:
    async with db.session() as s:
        rows = await s.execute(
            text(
                "SELECT payload->>'watched' FROM jobs WHERE kind = 'radar.poll_account' ORDER BY id"
            )
        )
        return [r[0] for r in rows]


async def _row(db: Database, public_id: str) -> Any:
    async with db.session() as s:
        return (
            await s.execute(
                text(
                    "SELECT since_cursor, next_poll_at, last_polled_at, last_post_at, "
                    "consecutive_empty, poll_interval_s, version FROM watched_accounts "
                    "WHERE public_id = :p"
                ),
                {"p": uuid.UUID(public_id)},
            )
        ).one()


async def _error_codes(db: Database) -> list[str]:
    await get_reporter().flush()
    async with db.session() as s:
        return list((await s.execute(text("SELECT code FROM error_reports"))).scalars())


# --- conversion ---------------------------------------------------------------


def test_to_item_input() -> None:
    created = NOW - timedelta(hours=1)
    item = ContentItem(
        platform="x",
        external_id="10",
        text="hello",
        author_external_id="7",
        author_handle="bob",
        lang_hint="fr",
        created_at=created,
        metrics=Metrics({"likes": 3}, NOW),
        parent_external_id="9",
        quoted_external_id="8",
        media=(MediaRef("image", "https://example.com/a.jpg"),),
    )
    out = to_item_input(item)
    assert (out.platform, out.external_id, out.text, out.lang_hint) == ("x", "10", "hello", "fr")
    assert out.author is not None
    assert (out.author.external_id, out.author.handle) == ("7", "bob")
    assert out.created_at == created
    assert dict(out.metrics) == {"likes": 3}
    assert (out.parent_external_id, out.quoted_external_id) == ("9", "8")
    assert [(m.kind, m.url) for m in out.media] == [("image", "https://example.com/a.jpg")]
    bare = to_item_input(ContentItem(platform="x", external_id="1", text="t"))
    assert bare.author is None and dict(bare.metrics) == {}


# --- client -------------------------------------------------------------------


def test_get_client_not_configured() -> None:
    with pytest.raises(PlatformNotConfigured):
        get_client(make_settings(x_bearer_token=None), "x")
    with pytest.raises(PlatformNotConfigured):
        get_client(make_settings(), "bluesky")


def test_get_client_builds_and_caches_x() -> None:
    settings = make_settings(x_bearer_token="secret-token")
    first = get_client(settings, "x")
    assert first.platform == "x"
    assert first.cost_per_read_usd_micros == 5_000
    assert first.cost_per_account_read_usd_micros == 10_000
    assert get_client(settings, "x") is first


# --- scheduler ----------------------------------------------------------------


async def test_schedule_priority_order_and_null_first(db: Database, settings: Settings) -> None:
    past = datetime.now(UTC) - timedelta(hours=1)
    low = await _watch(db, "1001", priority=-10)
    old = await _watch(db, "1002", priority=10, next_poll_at=past - timedelta(hours=1))
    recent = await _watch(db, "1003", priority=10, next_poll_at=past)
    fresh = await _watch(db, "1004", priority=10)
    high = await _watch(db, "1005", priority=90)
    assert await _schedule(db, settings) == 5
    assert await _queued(db) == [high, fresh, old, recent, low]


async def test_schedule_skips_not_due_inactive_pending_other_platform(
    db: Database, settings: Settings
) -> None:
    future = datetime.now(UTC) + timedelta(hours=1)
    due = await _watch(db, "2001")
    await _watch(db, "2002", next_poll_at=future)
    await _watch(db, "2003", active=False)
    await _watch(db, None, handle="pending")
    async with db.transaction() as s:
        await s.execute(
            text(
                "INSERT INTO platform_accounts (platform, external_id) "
                "VALUES ('bluesky', 'did:plc:abc')"
            )
        )
        await s.execute(
            text(
                "INSERT INTO watched_accounts (account_id, category, added_by) "
                "SELECT id, 'media_outlet', 'test' FROM platform_accounts "
                "WHERE platform = 'bluesky'"
            )
        )
    assert await _schedule(db, settings) == 1
    assert await _queued(db) == [due]


async def test_schedule_lease_and_no_duplicate(db: Database, settings: Settings) -> None:
    wid = await _watch(db, "3001", priority=60)
    assert await _schedule(db, settings) == 1
    row = await _row(db, wid)
    assert row.version == 1
    delta = (row.next_poll_at - datetime.now(UTC)).total_seconds()
    assert 100 < delta <= 121  # high-priority base, 120 s
    assert await _schedule(db, settings) == 0  # leased, and deduplicated anyway


async def test_schedule_batch_limit(db: Database) -> None:
    settings = make_settings(radar_schedule_batch=2)
    for i in range(4):
        await _watch(db, str(4000 + i))
    assert await _schedule(db, settings) == 2


async def test_schedule_creates_counter_with_configured_cap(
    db: Database, settings: Settings
) -> None:
    await _watch(db, "5001")
    await _schedule(db, settings)
    cap, spent, reserved = await _counter(db)
    assert (cap, spent, reserved) == (round(settings.x_monthly_cap_usd * 1e6), 0, 0)
    await _schedule(db, settings)  # idempotent
    assert await _one(db, "SELECT count(*) FROM budget_counters") == 1


async def test_schedule_counter_inherits_previous_cap(db: Database, settings: Settings) -> None:
    async with db.transaction() as s:
        await s.execute(
            text(
                "INSERT INTO budget_counters (scope, period_start, period_end, cap_usd_micros) "
                "VALUES ('provider:x', date_trunc('month', now() - interval '1 month')::date, "
                "date_trunc('month', now())::date, 7000000)"
            )
        )
    await _schedule(db, settings)
    assert (
        await _one(
            db,
            "SELECT cap_usd_micros FROM budget_counters "
            "WHERE scope = 'provider:x' AND period_start = date_trunc('month', now())::date",
        )
        == 7_000_000
    )


async def test_schedule_daily_cap(db: Database, settings: Settings) -> None:
    for i in range(5):
        await _watch(db, str(6000 + i))
    today = datetime.now(UTC).date()
    days_left = ((today.replace(day=28) + timedelta(days=4)).replace(day=1) - today).days
    # average read = 2 posts * 5000 = 10000 micros; allowance = exactly 3 polls today
    await _set_cap(db, 3 * 10_000 * days_left)
    assert await _schedule(db, settings) == 3
    assert await _schedule(db, settings) == 0  # the 3 enqueued today are deducted


async def test_schedule_exhausted_counter_enqueues_nothing(
    db: Database, settings: Settings
) -> None:
    await _watch(db, "7001")
    await _set_cap(db, 1_000_000, spent=1_000_000)
    assert await _schedule(db, settings) == 0


async def test_schedule_deducts_jobs_created_today(db: Database, settings: Settings) -> None:
    for i in range(3):
        await _watch(db, str(8000 + i))
    today = datetime.now(UTC).date()
    days_left = ((today.replace(day=28) + timedelta(days=4)).replace(day=1) - today).days
    await _set_cap(db, 2 * 10_000 * days_left)
    async with db.transaction() as s:
        await enqueue(s, "radar", "radar.poll_account", {"watched": "other"}, dedupe_key="x")
    assert await _schedule(db, settings) == 1


# --- poll job -----------------------------------------------------------------


async def test_poll_end_to_end(db: Database, settings: Settings) -> None:
    wid = await _watch(db, "5000", priority=0)
    await _set_cap(db, 1_000_000)
    fake = FakeClient([_page([_item("1002"), _item("1001")])])
    set_client_override("x", fake)
    await poll_account(_ctx(db, settings, "radar.poll_account", {"watched": wid}))

    assert fake.calls == [
        {"ref": "5000", "since": None, "max_results": 20, "token": None, "exclude": ["retweets"]}
    ]
    assert await _one(db, "SELECT count(*) FROM content_items") == 2
    assert await _one(db, "SELECT count(*) FROM observations WHERE source = 'radar'") == 2
    async with db.session() as s:
        events = await read_events(s, Cursor(0, 0), topics=["content.ingested"])
    assert len(events) == 2
    row = await _row(db, wid)
    assert row.since_cursor == "1002"
    assert row.consecutive_empty == 0
    assert row.version == 1
    assert row.last_polled_at is not None and row.last_post_at is not None
    assert row.poll_interval_s == 300  # 2 items: 600 // 2
    assert row.next_poll_at > datetime.now(UTC) + timedelta(seconds=250)
    assert await _counter(db) == (1_000_000, 10_000, 0)
    assert await _one(db, "SELECT count(*) FROM budget_reservations") == 0

    # Replay after commit: reads from the new since and finds nothing new.
    await poll_account(_ctx(db, settings, "radar.poll_account", {"watched": wid}))
    assert fake.calls[1]["since"] == "1002"
    assert await _one(db, "SELECT count(*) FROM content_items") == 2
    assert await _one(db, "SELECT count(*) FROM observations") == 2
    row = await _row(db, wid)
    assert row.since_cursor == "1002"
    assert row.consecutive_empty == 1
    assert row.poll_interval_s == 1200  # empty read: base * 2
    assert await _counter(db) == (1_000_000, 10_000, 0)


async def test_poll_first_read_is_a_single_page(db: Database, settings: Settings) -> None:
    wid = await _watch(db)
    fake = FakeClient([_page([_item("11")], next_token="more"), _page([_item("10")])])
    set_client_override("x", fake)
    await poll_account(_ctx(db, settings, "radar.poll_account", {"watched": wid}))
    assert len(fake.calls) == 1
    assert await _one(db, "SELECT count(*) FROM content_items") == 1


async def test_poll_paginates_with_since_up_to_max_pages(db: Database) -> None:
    settings = make_settings(radar_max_pages=2)
    wid = await _watch(db, since="100")
    await _set_cap(db, 10_000_000)
    fake = FakeClient(
        [
            _page([_item("203"), _item("202")], next_token="t1"),
            _page([_item("201")], next_token="t2"),
            _page([_item("200")]),
        ]
    )
    set_client_override("x", fake)
    await poll_account(_ctx(db, settings, "radar.poll_account", {"watched": wid}))
    assert [(c["since"], c["token"]) for c in fake.calls] == [("100", None), ("100", "t1")]
    assert await _one(db, "SELECT count(*) FROM content_items") == 3
    row = await _row(db, wid)
    assert row.since_cursor == "203"  # newest of the FIRST page
    assert await _counter(db) == (10_000_000, 15_000, 0)
    assert await _one(db, "SELECT count(*) FROM budget_reservations") == 0


async def test_poll_stops_when_no_next_token(db: Database, settings: Settings) -> None:
    wid = await _watch(db, since="100")
    fake = FakeClient([_page([_item("301")])])
    set_client_override("x", fake)
    await poll_account(_ctx(db, settings, "radar.poll_account", {"watched": wid}))
    assert len(fake.calls) == 1


async def test_poll_skips_unusable_accounts(db: Database, settings: Settings) -> None:
    fake = FakeClient()
    set_client_override("x", fake)
    inactive = await _watch(db, "9001", active=False)
    pending = await _watch(db, None, handle="pendinghandle")
    for payload in ({"watched": inactive}, {"watched": pending}, {"watched": str(uuid.uuid4())}):
        await poll_account(_ctx(db, settings, "radar.poll_account", payload))
    await poll_account(_ctx(db, settings, "radar.poll_account", {"watched": "not-a-uuid"}))
    assert fake.calls == []


async def test_poll_rate_limited_releases_reservation(db: Database, settings: Settings) -> None:
    wid = await _watch(db, since="100")
    await _set_cap(db, 1_000_000)
    set_client_override("x", FakeClient(error=PlatformRateLimited("slow", retry_after=42)))
    with pytest.raises(RetryLater) as info:
        await poll_account(_ctx(db, settings, "radar.poll_account", {"watched": wid}))
    assert info.value.delay == 42
    assert await _counter(db) == (1_000_000, 0, 0)
    assert await _one(db, "SELECT count(*) FROM budget_reservations") == 0


async def test_poll_error_after_first_page_charges_real_cost(
    db: Database, settings: Settings
) -> None:
    wid = await _watch(db, since="100")
    await _set_cap(db, 10_000_000)

    class Flaky(FakeClient):
        async def poll_page(self, account_ref: str, **kw: Any) -> PollPage:
            if self.calls:
                self.calls.append({})
                raise PlatformRateLimited("slow", retry_after=30)
            return await super().poll_page(account_ref, **kw)

    set_client_override("x", Flaky([_page([_item("401"), _item("400")], next_token="t")]))
    with pytest.raises(RetryLater):
        await poll_account(_ctx(db, settings, "radar.poll_account", {"watched": wid}))
    assert await _counter(db) == (10_000_000, 10_000, 0)
    assert (await _row(db, wid)).since_cursor == "100"  # nothing ingested, nothing advanced
    assert await _one(db, "SELECT count(*) FROM content_items") == 0


async def test_poll_budget_exhausted_does_not_call_client(db: Database, settings: Settings) -> None:
    wid = await _watch(db, since="100")
    await _set_cap(db, 1_000)  # below max_results * 5000
    fake = FakeClient([_page([_item("501")])])
    set_client_override("x", fake)
    with pytest.raises(RetryLater) as info:
        await poll_account(_ctx(db, settings, "radar.poll_account", {"watched": wid}))
    assert info.value.delay >= 60
    assert fake.calls == []
    assert await _counter(db) == (1_000, 0, 0)


async def test_poll_budget_exhausted_on_second_page_settles_first(db: Database) -> None:
    settings = make_settings(radar_max_results=20)
    wid = await _watch(db, since="100")
    await _set_cap(db, 150_000)  # one 100_000 hold fits, two do not
    fake = FakeClient([_page([_item("601")], next_token="t")])
    set_client_override("x", fake)
    with pytest.raises(RetryLater):
        await poll_account(_ctx(db, settings, "radar.poll_account", {"watched": wid}))
    assert len(fake.calls) == 1
    assert await _counter(db) == (150_000, 5_000, 0)


async def test_poll_auth_failed(db: Database, settings: Settings) -> None:
    wid = await _watch(db, since="100")
    await _set_cap(db, 1_000_000)
    set_client_override("x", FakeClient(error=PlatformAuthFailed("nope", status=401)))
    with pytest.raises(PermanentError):
        await poll_account(_ctx(db, settings, "radar.poll_account", {"watched": wid}))
    assert await _error_codes(db) == ["TWR-RAD-001"]
    assert await _counter(db) == (1_000_000, 0, 0)


async def test_poll_account_not_found_pushes_next_poll(db: Database, settings: Settings) -> None:
    wid = await _watch(db, since="100")
    set_client_override("x", FakeClient(error=PlatformBadResponse("gone", status=404)))
    await poll_account(_ctx(db, settings, "radar.poll_account", {"watched": wid}))
    assert await _error_codes(db) == ["TWR-RAD-006"]
    row = await _row(db, wid)
    assert row.next_poll_at > datetime.now(UTC) + timedelta(
        seconds=settings.radar_interval_max_s - 60
    )
    assert row.version == 1


async def test_poll_other_bad_response_is_permanent(db: Database, settings: Settings) -> None:
    wid = await _watch(db, since="100")
    set_client_override("x", FakeClient(error=PlatformBadResponse("weird", status=400)))
    with pytest.raises(PermanentError):
        await poll_account(_ctx(db, settings, "radar.poll_account", {"watched": wid}))
    assert await _error_codes(db) == ["TWR-RAD-004"]


async def test_poll_unavailable_is_captured_and_reraised(db: Database, settings: Settings) -> None:
    wid = await _watch(db, since="100")
    set_client_override("x", FakeClient(error=PlatformUnavailable("5xx", status=503)))
    with pytest.raises(PlatformUnavailable):
        await poll_account(_ctx(db, settings, "radar.poll_account", {"watched": wid}))
    assert await _error_codes(db) == ["TWR-RAD-003"]


async def test_poll_not_configured(db: Database) -> None:
    settings = make_settings(x_bearer_token=None)
    wid = await _watch(db, since="100")
    with pytest.raises(PermanentError):
        await poll_account(_ctx(db, settings, "radar.poll_account", {"watched": wid}))
    assert await _error_codes(db) == ["TWR-RAD-007"]


async def test_poll_expired_hold_is_not_released_twice(db: Database, settings: Settings) -> None:
    # A hold released by maintenance before the settlement must not be given back twice.
    wid = await _watch(db, since="100")
    await _set_cap(db, 1_000_000)

    class Slow(FakeClient):
        async def poll_page(self, account_ref: str, **kw: Any) -> PollPage:
            async with db.transaction() as s:
                await s.execute(text("UPDATE budget_reservations SET expires_at = now()"))
                await release_expired(s, datetime.now(UTC) + timedelta(seconds=1))
            return await super().poll_page(account_ref, **kw)

    set_client_override("x", Slow([_page([_item("701")])]))
    await poll_account(_ctx(db, settings, "radar.poll_account", {"watched": wid}))
    assert await _counter(db) == (1_000_000, 5_000, 0)


# --- resolution ---------------------------------------------------------------


async def _events(db: Database, topic: str = "watchlist.changed") -> list[Any]:
    async with db.session() as s:
        return await read_events(s, Cursor(0, 0), topics=[topic])


async def test_subscription_filters_events(db: Database, settings: Settings) -> None:
    pending = await _watch(db, None, handle="pendingone")
    await _watch(db, "1234")  # already resolved: ignored
    inactive = await _watch(db, None, handle="inactiveone")
    async with db.transaction() as s:
        await uc.update_watched_account(s, EDITOR, uuid.UUID(inactive), version=1, active=False)
    events = await _events(db)
    assert [e.payload["action"] for e in events] == ["added", "added", "added", "updated"]
    # Only the events that carry a pending, active account (and an added/updated action).
    relevant = [e for e in events if e.payload["watched_account"]["public_id"] != inactive] + [
        events[-1]
    ]
    async with db.transaction() as s:
        await on_watchlist_changed(s, relevant)
        await on_watchlist_changed(s, relevant)  # dedupe: no second job
    async with db.session() as s:
        rows = (
            await s.execute(text("SELECT kind, queue, payload->>'watched', dedupe_key FROM jobs"))
        ).all()
    assert [tuple(r) for r in rows] == [
        ("radar.resolve_account", "radar", pending, f"radar:resolve:{pending}")
    ]


async def test_resolve_flow(db: Database, settings: Settings) -> None:
    await _set_cap(db, 1_000_000)
    wid = await _watch(db, None, handle="Jack")
    events = await _events(db)
    async with db.transaction() as s:
        await on_watchlist_changed(s, events)
    assert await _one(db, "SELECT count(*) FROM jobs WHERE kind = 'radar.resolve_account'") == 1

    fake = FakeClient(account=PublicAccount("x", "7777", handle="jack"))
    set_client_override("x", fake)
    await resolve_account(_ctx(db, settings, "radar.resolve_account", {"watched": wid}))
    assert fake.account_calls == ["Jack"]
    assert (
        await _one(
            db,
            "SELECT a.external_id FROM watched_accounts w JOIN platform_accounts a "
            "ON a.id = w.account_id WHERE w.public_id = :p",
            p=uuid.UUID(wid),
        )
        == "7777"
    )
    actions = [e.payload["action"] for e in await _events(db)]
    assert actions == ["added", "resolved"]
    assert await _counter(db) == (1_000_000, 10_000, 0)

    # Already resolved: nothing more.
    await resolve_account(_ctx(db, settings, "radar.resolve_account", {"watched": wid}))
    assert fake.account_calls == ["Jack"]


async def test_resolve_not_found(db: Database, settings: Settings) -> None:
    await _set_cap(db, 1_000_000)
    wid = await _watch(db, None, handle="ghost")
    set_client_override("x", FakeClient(account=None))
    await resolve_account(_ctx(db, settings, "radar.resolve_account", {"watched": wid}))
    assert await _error_codes(db) == ["TWR-RAD-006"]
    assert await _counter(db) == (1_000_000, 0, 0)
    assert [e.payload["action"] for e in await _events(db)] == ["added"]


async def test_resolve_conflict_still_settles(db: Database, settings: Settings) -> None:
    await _set_cap(db, 1_000_000)
    await _watch(db, "7777")  # target id already watched
    wid = await _watch(db, None, handle="dup")
    set_client_override("x", FakeClient(account=PublicAccount("x", "7777", handle="dup")))
    await resolve_account(_ctx(db, settings, "radar.resolve_account", {"watched": wid}))
    assert await _counter(db) == (1_000_000, 10_000, 0)
    assert await _one(db, "SELECT count(*) FROM budget_reservations") == 0
    assert (
        await _one(
            db,
            "SELECT a.external_id IS NULL FROM watched_accounts w JOIN platform_accounts a "
            "ON a.id = w.account_id WHERE w.public_id = :p",
            p=uuid.UUID(wid),
        )
        is True
    )


async def test_resolve_rate_limited(db: Database, settings: Settings) -> None:
    await _set_cap(db, 1_000_000)
    wid = await _watch(db, None, handle="slowone")
    set_client_override("x", FakeClient(error=PlatformRateLimited("slow", retry_after=20)))
    with pytest.raises(RetryLater):
        await resolve_account(_ctx(db, settings, "radar.resolve_account", {"watched": wid}))
    assert await _counter(db) == (1_000_000, 0, 0)


# --- wiring -------------------------------------------------------------------


async def test_service_declaration() -> None:
    services = load_services(["ping", "ingest", "radar"])
    services.validate(lease_seconds=300)
    assert service.name == "radar"
    assert {"radar.poll_account", "radar.resolve_account"} <= set(services.handlers)
    assert all(
        services.handlers[k].queue == "radar" for k in services.handlers if k.startswith("radar.")
    )
    assert ("radar.watchlist", service.subscriptions[0]) in services.subscriptions
    assert service.subscriptions[0].topics == ("watchlist.changed",)
    assert {t.name: t.every_seconds for t in services.periodic if t.name.startswith("radar")} == {
        "radar:schedule": 60
    }


async def test_periodic_under_singleton(db: Database, settings: Settings) -> None:
    await _watch(db, "9100")

    async def run(s: Any) -> int:
        return int(await service.periodic[0].fn(s, settings))

    assert await run_singleton(db, service.periodic[0].name, run) == 1


def _loaded_after(code: str, modules: tuple[str, ...]) -> list[str]:
    script = f"import sys\n{code}\nprint(','.join(m for m in {modules!r} if m in sys.modules))\n"
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script], capture_output=True, text=True, check=True, timeout=60
    )
    return [m for m in out.stdout.strip().split(",") if m]


@pytest.mark.parametrize("names", [["ping", "ingest"], ["ping", "ingest", "radar"]])
def test_radar_import_isolation(names: list[str]) -> None:
    code = (
        "import tower.hangar.main\n"
        "from tower.core.services.registry import load_services\n"
        f"load_services({names!r}).validate(lease_seconds=300)\n"
    )
    heavy = ("httpx", "tower.core.adapters.x_client", "fastapi")
    assert _loaded_after(code, heavy) == []
