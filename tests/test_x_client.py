from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from tower.core.adapters.x_client import (
    MEDIA_FIELDS,
    POST_EXPANSIONS,
    POST_FIELDS,
    USER_FIELDS,
    XPlatformClient,
    parse_post,
)
from tower.core.ports.platforms import (
    ContentItem,
    PlatformAuthFailed,
    PlatformBadResponse,
    PlatformError,
    PlatformRateLimited,
    PlatformUnavailable,
)

TOKEN = "SECRET-TOKEN-xyz"

Handler = Callable[[httpx.Request], httpx.Response]


def make(handler: Handler) -> tuple[XPlatformClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    http = httpx.AsyncClient(base_url="https://api.x.com", transport=httpx.MockTransport(wrapped))
    return XPlatformClient(TOKEN, http), seen


def ok(body: dict[str, Any]) -> Handler:
    return lambda request: httpx.Response(200, json=body)


TIMELINE: dict[str, Any] = {
    "data": [
        {
            "id": "103",
            "text": "short text truncated…",
            "author_id": "42",
            "created_at": "2026-11-10T12:30:00.000Z",
            "lang": "fr",
            "public_metrics": {
                "repost_count": 1,
                "reply_count": 2,
                "like_count": 3,
                "quote_count": 4,
                "bookmark_count": 5,
                "impression_count": 600,
            },
            "note_post": {"text": "The full long text of the post."},
            "attachments": {"media_keys": ["3_1", "7_2", "9_9"]},
        },
        {
            "id": "102",
            "text": "A reply with a quote",
            "author_id": "42",
            "created_at": "2026-11-10T11:00:00Z",
            "lang": "en",
            "referenced_posts": [
                {"type": "replied_to", "id": "90"},
                {"type": "quoted", "id": "91"},
            ],
        },
        {"id": "101", "text": "no language", "author_id": "42", "lang": "und"},
    ],
    "includes": {
        "media": [
            {"media_key": "3_1", "type": "photo", "url": "https://pbs.example/p.jpg"},
            {
                "media_key": "7_2",
                "type": "video",
                "preview_image_url": "https://pbs.example/preview.jpg",
            },
            {"media_key": "9_9", "type": "photo"},
        ],
        "posts": [{"id": "90", "text": "parent"}],
        "users": [{"id": "42", "username": "someone", "name": "Some One"}],
    },
    "meta": {"newest_id": "103", "oldest_id": "101", "next_token": "NT1", "result_count": 3},
}


async def test_poll_page_conversion_and_request() -> None:
    client, seen = make(ok(TIMELINE))
    page = await client.poll_page("42", since="100", max_results=50)
    req = seen[0]
    assert req.url.path == "/2/users/42/tweets"
    q = req.url.params
    assert q["post.fields"] == POST_FIELDS
    assert q["expansions"] == POST_EXPANSIONS
    assert q["media.fields"] == MEDIA_FIELDS
    assert q["since_id"] == "100"
    assert q["max_results"] == "50"
    assert q["exclude"] == "retweets"
    assert "pagination_token" not in q
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert req.headers["user-agent"] == "Seat12A-Tower/0.1"

    assert page.newest_id == "103"
    assert page.next_token == "NT1"
    assert page.usage.posts == 4  # 3 data + 1 includes.posts
    assert page.usage.users == 1
    first, second, third = page.items
    assert first.platform == "x"
    assert first.external_id == "103"
    assert first.text == "The full long text of the post."
    assert first.author_external_id == "42"
    assert first.author_handle == "someone"
    assert first.lang_hint == "fr"
    assert first.created_at is not None
    assert first.created_at.isoformat() == "2026-11-10T12:30:00+00:00"
    assert first.metrics is not None
    assert dict(first.metrics.values) == {
        "repost_count": 1,
        "reply_count": 2,
        "like_count": 3,
        "quote_count": 4,
        "bookmark_count": 5,
        "impression_count": 600,
    }
    assert first.metrics.measured_at.tzinfo is not None
    assert [(m.kind, m.url) for m in first.media] == [
        ("image", "https://pbs.example/p.jpg"),
        ("video", "https://pbs.example/preview.jpg"),
    ]
    assert second.parent_external_id == "90"
    assert second.quoted_external_id == "91"
    assert second.metrics is None
    assert second.media == ()
    assert third.lang_hint is None
    assert third.created_at is None


async def test_poll_page_pagination_token_exclude_and_clamp() -> None:
    client, seen = make(ok(TIMELINE))
    await client.poll_page("42", since=None, max_results=1, pagination_token="PT", exclude=())
    q = seen[0].url.params
    assert q["max_results"] == "5"
    assert q["pagination_token"] == "PT"
    assert "exclude" not in q
    assert "since_id" not in q
    await client.poll_page("42", since=None, max_results=500, exclude=("retweets", "replies"))
    assert seen[1].url.params["max_results"] == "100"
    assert seen[1].url.params["exclude"] == "retweets,replies"


async def test_poll_page_non_numeric_account_raises() -> None:
    client, seen = make(ok(TIMELINE))
    with pytest.raises(ValueError, match="numeric"):
        await client.poll_page("someone", since=None, max_results=10)
    assert seen == []


async def test_poll_page_empty() -> None:
    client, _ = make(ok({"meta": {"result_count": 0}}))
    page = await client.poll_page("42", since="1", max_results=10)
    assert page.items == ()
    assert page.newest_id is None
    assert page.next_token is None
    assert page.usage.posts == 0 and page.usage.users == 0


async def test_poll_page_newest_id_fallback() -> None:
    body = {"data": [{"id": "7", "text": "x"}], "meta": {"result_count": 1}}
    client, _ = make(ok(body))
    page = await client.poll_page("42", since=None, max_results=10)
    assert page.newest_id == "7"
    assert page.next_token is None


async def test_poll_account_follows_three_pages_max() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("pagination_token")
        n = {None: 1, "T1": 2, "T2": 3, "T3": 4}[token]
        return httpx.Response(
            200,
            json={
                "data": [{"id": str(100 - n), "text": f"p{n}"}],
                "meta": {"newest_id": str(100 - n), "next_token": f"T{n}", "result_count": 1},
            },
        )

    client, seen = make(handler)
    items = await client.poll_account("42", since="1")
    assert [i.text for i in items] == ["p1", "p2", "p3"]
    assert len(seen) == 3
    assert all(r.url.params["max_results"] == "100" for r in seen)
    assert seen[2].url.params["pagination_token"] == "T2"


async def test_poll_account_stops_without_next_token() -> None:
    body = {"data": [{"id": "5", "text": "a"}], "meta": {"result_count": 1}}
    client, seen = make(ok(body))
    items = await client.poll_account("42", since=None)
    assert len(items) == 1 and len(seen) == 1


async def test_get_account_by_username_and_id() -> None:
    body = {
        "data": {
            "id": "42",
            "username": "someone",
            "name": "Some One",
            "description": "bio",
            "verified": True,
            "public_metrics": {"followers_count": 1234},
        }
    }
    client, seen = make(ok(body))
    account = await client.get_account("@someone")
    assert seen[0].url.path == "/2/users/by/username/someone"
    assert seen[0].url.params["user.fields"] == USER_FIELDS
    assert account is not None
    assert (account.platform, account.external_id, account.handle) == ("x", "42", "someone")
    assert account.display_name == "Some One"
    assert account.description == "bio"
    assert account.verified is True
    assert account.followers == 1234
    await client.get_account("42")
    assert seen[1].url.path == "/2/users/42"


@pytest.mark.parametrize("ref", ["", "@", "bad handle", "way_too_long_handle_x", "a-b", "é"])
async def test_get_account_invalid_handle_makes_no_call(ref: str) -> None:
    client, seen = make(ok({}))
    assert await client.get_account(ref) is None
    assert seen == []


async def test_get_account_not_found_variants() -> None:
    client, _ = make(lambda r: httpx.Response(404, json={}))
    assert await client.get_account("someone") is None
    errors = {
        "errors": [
            {
                "value": "nobody",
                "detail": "Could not find user",
                "title": "Not Found Error",
                "type": "https://api.twitter.com/2/problems/resource-not-found",
            }
        ]
    }
    client, _ = make(ok(errors))
    assert await client.get_account("nobody") is None


async def test_get_item_and_metrics() -> None:
    body = {"data": TIMELINE["data"][0], "includes": TIMELINE["includes"]}
    client, seen = make(ok(body))
    item = await client.get_item("103")
    assert seen[0].url.path == "/2/tweets/103"
    assert seen[0].url.params["post.fields"] == POST_FIELDS
    assert seen[0].url.params["expansions"] == POST_EXPANSIONS
    assert seen[0].url.params["media.fields"] == MEDIA_FIELDS
    assert item is not None and item.external_id == "103"
    metrics = await client.get_metrics("103")
    assert metrics is not None and metrics.values["like_count"] == 3


async def test_get_item_not_found_and_non_numeric() -> None:
    client, seen = make(lambda r: httpx.Response(404))
    assert await client.get_item("5") is None
    assert await client.get_metrics("5") is None
    n = len(seen)
    assert await client.get_item("abc") is None
    assert len(seen) == n
    client, _ = make(ok({"errors": [{"title": "Not Found Error", "type": "x"}]}))
    assert await client.get_item("5") is None


async def test_poll_page_not_found_raises_bad_response() -> None:
    client, _ = make(lambda r: httpx.Response(404))
    with pytest.raises(PlatformBadResponse) as exc:
        await client.poll_page("42", since=None, max_results=10)
    assert exc.value.status == 404
    client, _ = make(
        ok({"errors": [{"type": "https://api.twitter.com/2/problems/resource-not-found"}]})
    )
    with pytest.raises(PlatformBadResponse) as exc2:
        await client.poll_page("42", since=None, max_results=10)
    assert exc2.value.status == 404


def _item(**kw: Any) -> ContentItem:
    return ContentItem(platform="x", external_id="200", text="t", **kw)


async def test_thread_context_one_grouped_call() -> None:
    body = {
        "data": [
            {"id": "90", "text": "parent"},
            {"id": "91", "text": "quoted"},
        ]
    }
    client, seen = make(ok(body))
    ctx = await client.get_thread_context(_item(parent_external_id="90", quoted_external_id="91"))
    assert len(seen) == 1
    assert seen[0].url.path == "/2/tweets"
    assert seen[0].url.params["ids"] == "90,91"
    assert seen[0].url.params["post.fields"] == POST_FIELDS
    assert ctx.parent is not None and ctx.parent.text == "parent"
    assert ctx.quoted is not None and ctx.quoted.text == "quoted"


async def test_thread_context_dedup_and_missing() -> None:
    client, seen = make(ok({"data": [{"id": "90", "text": "p"}]}))
    ctx = await client.get_thread_context(_item(parent_external_id="90", quoted_external_id="90"))
    assert seen[0].url.params["ids"] == "90"
    assert ctx.parent is not None and ctx.quoted is not None
    client, _ = make(
        ok({"data": [{"id": "90", "text": "p"}], "errors": [{"title": "Not Found Error"}]})
    )
    ctx = await client.get_thread_context(_item(parent_external_id="90", quoted_external_id="91"))
    assert ctx.parent is not None and ctx.quoted is None


async def test_thread_context_no_reference_no_call() -> None:
    client, seen = make(ok({}))
    ctx = await client.get_thread_context(_item())
    assert ctx.parent is None and ctx.quoted is None
    assert seen == []


async def test_rate_limited_with_reset_header() -> None:
    reset = int(time.time()) + 120
    client, _ = make(
        lambda r: httpx.Response(429, headers={"x-rate-limit-reset": str(reset)}, json={})
    )
    with pytest.raises(PlatformRateLimited) as exc:
        await client.get_item("5")
    assert 100 <= exc.value.retry_after <= 121
    assert exc.value.status == 429


async def test_rate_limited_clamped_and_default() -> None:
    far = int(time.time()) + 100_000
    client, _ = make(lambda r: httpx.Response(429, headers={"x-rate-limit-reset": str(far)}))
    with pytest.raises(PlatformRateLimited) as exc:
        await client.get_item("5")
    assert exc.value.retry_after == 3600
    past = int(time.time()) - 500
    client, _ = make(lambda r: httpx.Response(429, headers={"x-rate-limit-reset": str(past)}))
    with pytest.raises(PlatformRateLimited) as exc:
        await client.get_item("5")
    assert exc.value.retry_after == 1
    client, _ = make(lambda r: httpx.Response(429))
    with pytest.raises(PlatformRateLimited) as exc:
        await client.get_item("5")
    assert exc.value.retry_after == 60
    client, _ = make(lambda r: httpx.Response(429, headers={"x-rate-limit-reset": "soon"}))
    with pytest.raises(PlatformRateLimited) as exc:
        await client.get_item("5")
    assert exc.value.retry_after == 60


def _failing_handlers() -> list[tuple[Handler, type[PlatformError]]]:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    return [
        (lambda r: httpx.Response(401), PlatformAuthFailed),
        (lambda r: httpx.Response(403), PlatformAuthFailed),
        (lambda r: httpx.Response(429), PlatformRateLimited),
        (lambda r: httpx.Response(500), PlatformUnavailable),
        (lambda r: httpx.Response(503), PlatformUnavailable),
        (boom, PlatformUnavailable),
        (timeout, PlatformUnavailable),
        (lambda r: httpx.Response(400, json={}), PlatformBadResponse),
        (lambda r: httpx.Response(200, content=b"not json"), PlatformBadResponse),
        (lambda r: httpx.Response(200, json=[1, 2]), PlatformBadResponse),
        (lambda r: httpx.Response(200, json={"data": {"id": "5"}}), PlatformBadResponse),
    ]


@pytest.mark.parametrize("index", range(11))
async def test_error_mapping_and_no_token_leak(index: int) -> None:
    handler, expected = _failing_handlers()[index]
    client, _ = make(handler)
    with pytest.raises(expected) as exc:
        await client.get_item("5")
    assert TOKEN not in str(exc.value)
    assert TOKEN not in repr(exc.value)
    assert "?" not in str(exc.value)
    assert TOKEN not in repr(client)
    assert TOKEN not in repr(client.__dict__.get("_http"))


async def test_status_attached_to_errors() -> None:
    client, _ = make(lambda r: httpx.Response(403))
    with pytest.raises(PlatformAuthFailed) as exc:
        await client.get_item("5")
    assert exc.value.status == 403
    client, _ = make(lambda r: httpx.Response(418))
    with pytest.raises(PlatformBadResponse) as exc2:
        await client.get_item("5")
    assert exc2.value.status == 418


def test_parse_post_malformed_and_legacy_keys() -> None:
    with pytest.raises(PlatformBadResponse):
        parse_post({"text": "no id"}, {})
    with pytest.raises(PlatformBadResponse):
        parse_post({"id": "1"}, {})
    item = parse_post(
        {
            "id": 5,
            "text": "t",
            "referenced_tweets": [{"type": "replied_to", "id": 4}],
            "note_tweet": {"text": "long"},
        },
        {},
    )
    assert item.external_id == "5"
    assert item.text == "long"
    assert item.parent_external_id == "4"
    assert item.author_handle is None


async def test_prices_and_aclose_ownership() -> None:
    client, _ = make(ok({}))
    assert client.cost_per_read_usd_micros == 5_000
    assert client.cost_per_account_read_usd_micros == 10_000
    http = httpx.AsyncClient(transport=httpx.MockTransport(ok({})))
    custom = XPlatformClient(TOKEN, http, post_read_usd_micros=1, user_read_usd_micros=2)
    assert (custom.cost_per_read_usd_micros, custom.cost_per_account_read_usd_micros) == (1, 2)
    await custom.aclose()
    assert not http.is_closed
    await http.aclose()
    owned = XPlatformClient(TOKEN)
    await owned.aclose()


def test_import_does_not_load_httpx() -> None:
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import tower.core.adapters.x_client, sys; print('httpx' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "False"
