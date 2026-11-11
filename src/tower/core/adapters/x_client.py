"""X (Twitter) official API v2 adapter: implements ``PollingPlatformClient``.

Names checked against the official OpenAPI spec (https://api.x.com/2/openapi.json,
version 2.174, 2026-11-11) and docs.x.com: see docs/radar.md, "Vérifications".
httpx is imported lazily (in ``__init__``): importing this module stays cheap.
The bearer token is never logged nor put in an error.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from tower.core.ports.platforms import (
    ContentItem,
    MediaRef,
    Metrics,
    PlatformAuthFailed,
    PlatformBadResponse,
    PlatformRateLimited,
    PlatformUnavailable,
    PollPage,
    PublicAccount,
    ReadUsage,
    ThreadContext,
)

if TYPE_CHECKING:
    import httpx

__all__ = ["XPlatformClient"]

DEFAULT_API_BASE = "https://api.x.com"

#: Query parameters of post reads (official spec: ``post.fields``, ``referenced_posts``).
POST_FIELDS = (
    "author_id,conversation_id,created_at,lang,public_metrics,referenced_posts,"
    "attachments,note_post,entities"
)
POST_EXPANSIONS = "attachments.media_keys"
MEDIA_FIELDS = "type,url,preview_image_url"
USER_FIELDS = "username,name,description,verified,public_metrics"


USER_AGENT = "Seat12A-Tower/0.1"
_NUMERIC_ID = re.compile(r"[0-9]{1,19}")
_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")
_METRIC_KEYS = (
    "repost_count",
    "reply_count",
    "like_count",
    "quote_count",
    "bookmark_count",
    "impression_count",
)
_MAX_PAGES = 3
_DEFAULT_RETRY_AFTER = 60.0


def _is_not_found_errors(errors: Any) -> bool:
    if not isinstance(errors, list):
        return False
    for err in errors:
        if not isinstance(err, dict):
            continue
        if (
            "resource-not-found" in str(err.get("type", ""))
            or err.get("title") == "Not Found Error"
        ):
            return True
    return False


def _retry_after(headers: Any) -> float:
    try:
        reset = float(headers.get("x-rate-limit-reset", ""))
    except (TypeError, ValueError):
        return _DEFAULT_RETRY_AFTER
    return min(max(reset - time.time(), 1.0), 3600.0)


class XPlatformClient:
    """X API v2, app-only bearer token. One instance per process (shared httpx client).

    ``http``: optional ``httpx.AsyncClient`` (tests pass one built on
    ``httpx.MockTransport``); otherwise one is created with ``base_url=api_base``.
    """

    platform = "x"

    def __init__(
        self,
        bearer_token: str,
        http: httpx.AsyncClient | None = None,
        *,
        api_base: str = DEFAULT_API_BASE,
        post_read_usd_micros: int = 5_000,
        user_read_usd_micros: int = 10_000,
        timeout: float = 15.0,
    ) -> None:
        import httpx

        self._httpx = httpx
        self._headers = {"Authorization": f"Bearer {bearer_token}", "User-Agent": USER_AGENT}
        self._owns_http = http is None
        self._http: httpx.AsyncClient = (
            http
            if http is not None
            else httpx.AsyncClient(base_url=api_base, timeout=timeout, headers=self._headers)
        )
        self.cost_per_read_usd_micros = post_read_usd_micros
        self.cost_per_account_read_usd_micros = user_read_usd_micros

    cost_per_read_usd_micros: int
    cost_per_account_read_usd_micros: int

    def __repr__(self) -> str:
        return "XPlatformClient()"

    async def aclose(self) -> None:
        """Close the httpx client if this instance created it."""
        if self._owns_http:
            await self._http.aclose()

    async def _get(self, name: str, path: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """GET and decode; None means "not found" (404, or 200 with not-found errors only)."""
        httpx = self._httpx
        try:
            resp = await self._http.get(path, params=params, headers=self._headers)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise PlatformUnavailable(f"X API transport error on {name}") from exc
        status = resp.status_code
        if status in (401, 403):
            raise PlatformAuthFailed(f"X API {status} on {name}", status=status)
        if status == 429:
            raise PlatformRateLimited(
                f"X API 429 on {name}", retry_after=_retry_after(resp.headers)
            )
        if status == 404:
            return None
        if status >= 500:
            raise PlatformUnavailable(f"X API {status} on {name}", status=status)
        if status >= 400:
            raise PlatformBadResponse(f"X API {status} on {name}", status=status)
        try:
            body = resp.json()
        except ValueError as exc:
            raise PlatformBadResponse(f"X API invalid JSON on {name}", status=status) from exc
        if not isinstance(body, dict):
            raise PlatformBadResponse(f"X API unexpected payload on {name}", status=status)
        if "data" not in body and _is_not_found_errors(body.get("errors")):
            return None
        return body

    @staticmethod
    def _post_params() -> dict[str, Any]:
        return {
            "post.fields": POST_FIELDS,
            "expansions": POST_EXPANSIONS,
            "media.fields": MEDIA_FIELDS,
        }

    async def get_account(self, account_ref: str) -> PublicAccount | None:
        """``GET /2/users/{id}`` if ``account_ref`` is numeric, else
        ``GET /2/users/by/username/{username}`` (leading ``@`` stripped). 404 or a
        ``Not Found`` problem in ``errors`` without ``data`` -> None."""
        ref = account_ref.strip()
        if _NUMERIC_ID.fullmatch(ref):
            path = f"/2/users/{ref}"
        else:
            handle = ref.removeprefix("@")
            if not _HANDLE.fullmatch(handle):
                return None
            path = f"/2/users/by/username/{handle}"
        body = await self._get("users.get", path, {"user.fields": USER_FIELDS})
        if body is None:
            return None
        data = body.get("data")
        if not isinstance(data, dict) or data.get("id") is None:
            raise PlatformBadResponse("X API malformed user", status=200)
        metrics = data.get("public_metrics")
        followers = metrics.get("followers_count") if isinstance(metrics, dict) else None
        return PublicAccount(
            platform="x",
            external_id=str(data["id"]),
            handle=data.get("username"),
            display_name=data.get("name"),
            description=data.get("description"),
            verified=data.get("verified"),
            followers=followers if isinstance(followers, int) else None,
        )

    async def poll_page(
        self,
        account_ref: str,
        *,
        since: str | None,
        max_results: int,
        pagination_token: str | None = None,
        exclude: Sequence[str] = ("retweets",),
    ) -> PollPage:
        """``GET /2/users/{id}/tweets`` (``account_ref`` = numeric user id) with
        ``since_id``, ``max_results`` clamped to 5..100, ``pagination_token``."""
        if not _NUMERIC_ID.fullmatch(account_ref):
            raise ValueError("account_ref must be a numeric X user id")
        params = self._post_params()
        params["max_results"] = min(max(max_results, 5), 100)
        if since:
            params["since_id"] = since
        if pagination_token:
            params["pagination_token"] = pagination_token
        if exclude:
            params["exclude"] = ",".join(exclude)
        body = await self._get("users.tweets", f"/2/users/{account_ref}/tweets", params)
        if body is None:
            raise PlatformBadResponse("X API 404 on users.tweets", status=404)
        data = body.get("data")
        meta = body.get("meta")
        meta = meta if isinstance(meta, dict) else {}
        if not data:
            return PollPage((), None, None, ReadUsage())
        if not isinstance(data, list):
            raise PlatformBadResponse("X API malformed timeline", status=200)
        includes = _includes(body)
        items = tuple(parse_post(raw, includes) for raw in data if isinstance(raw, dict))
        newest = meta.get("newest_id")
        next_token = meta.get("next_token")
        return PollPage(
            items=items,
            newest_id=str(newest) if newest else (items[0].external_id if items else None),
            next_token=str(next_token) if next_token else None,
            usage=_usage(len(data), includes),
        )

    async def poll_account(self, account_ref: str, *, since: str | None) -> Sequence[ContentItem]:
        """All pages since ``since`` (at most 3 pages of 100), newest first."""
        items: list[ContentItem] = []
        token: str | None = None
        for _ in range(_MAX_PAGES):
            page = await self.poll_page(
                account_ref, since=since, max_results=100, pagination_token=token
            )
            items.extend(page.items)
            token = page.next_token
            if token is None:
                break
        return items

    async def _get_post(self, external_id: str) -> ContentItem | None:
        if not _NUMERIC_ID.fullmatch(external_id):
            return None
        body = await self._get("tweets.get", f"/2/tweets/{external_id}", self._post_params())
        if body is None:
            return None
        data = body.get("data")
        if not isinstance(data, dict):
            raise PlatformBadResponse("X API malformed post", status=200)
        return parse_post(data, _includes(body))

    async def get_item(self, external_id: str) -> ContentItem | None:
        """``GET /2/tweets/{id}``; 404 -> None."""
        return await self._get_post(external_id)

    async def get_metrics(self, external_id: str) -> Metrics | None:
        """Public metrics of one post (same call as ``get_item``)."""
        item = await self._get_post(external_id)
        return item.metrics if item else None

    async def get_thread_context(self, item: ContentItem) -> ThreadContext:
        """Parent and quoted posts in ONE ``GET /2/tweets?ids=a,b`` call (no call when
        the item references nothing)."""
        ids = [
            i
            for i in dict.fromkeys((item.parent_external_id, item.quoted_external_id))
            if i is not None
        ]
        if not ids:
            return ThreadContext()
        params = self._post_params()
        params["ids"] = ",".join(ids)
        body = await self._get("tweets.lookup", "/2/tweets", params)
        if body is None:
            return ThreadContext()
        data = body.get("data")
        if data is None:
            data = []
        if not isinstance(data, list):
            raise PlatformBadResponse("X API malformed lookup", status=200)
        includes = _includes(body)
        by_id: dict[str, ContentItem] = {}
        for raw in data:
            if isinstance(raw, dict):
                parsed = parse_post(raw, includes)
                by_id[parsed.external_id] = parsed
        return ThreadContext(
            parent=by_id.get(item.parent_external_id) if item.parent_external_id else None,
            quoted=by_id.get(item.quoted_external_id) if item.quoted_external_id else None,
        )


def _includes(body: dict[str, Any]) -> dict[str, Any]:
    inc = body.get("includes")
    return inc if isinstance(inc, dict) else {}


def _usage(n_posts: int, includes: dict[str, Any]) -> ReadUsage:
    posts = includes.get("posts") or includes.get("tweets") or []
    users = includes.get("users") or []
    return ReadUsage(
        posts=n_posts + (len(posts) if isinstance(posts, list) else 0),
        users=len(users) if isinstance(users, list) else 0,
    )


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_post(raw: dict[str, Any], includes: dict[str, Any]) -> ContentItem:
    """Convert one API post (+ the response ``includes``) into a ``ContentItem``."""
    post_id = raw.get("id")
    text = raw.get("text")
    if post_id is None or not isinstance(text, str):
        raise PlatformBadResponse("X API malformed post (missing id or text)", status=200)
    note = raw.get("note_post") or raw.get("note_tweet")
    if isinstance(note, dict) and isinstance(note.get("text"), str) and note["text"]:
        text = note["text"]

    author_id = raw.get("author_id")
    author_id = str(author_id) if author_id is not None else None
    handle: str | None = None
    users = includes.get("users")
    if author_id is not None and isinstance(users, list):
        for user in users:
            if isinstance(user, dict) and str(user.get("id")) == author_id:
                handle = user.get("username")
                break

    lang = raw.get("lang")
    lang_hint = lang if isinstance(lang, str) and lang and lang != "und" else None

    metrics: Metrics | None = None
    pm = raw.get("public_metrics")
    if isinstance(pm, dict):
        values = {k: pm[k] for k in _METRIC_KEYS if isinstance(pm.get(k), int)}
        metrics = Metrics(values=values, measured_at=datetime.now(UTC))

    parent: str | None = None
    quoted: str | None = None
    refs = raw.get("referenced_posts") or raw.get("referenced_tweets") or []
    for ref in refs if isinstance(refs, list) else []:
        if not isinstance(ref, dict) or ref.get("id") is None:
            continue
        if ref.get("type") == "replied_to":
            parent = str(ref["id"])
        elif ref.get("type") == "quoted":
            quoted = str(ref["id"])

    media: list[MediaRef] = []
    attachments = raw.get("attachments")
    keys = attachments.get("media_keys") if isinstance(attachments, dict) else None
    pool = includes.get("media")
    if isinstance(keys, list) and isinstance(pool, list):
        by_key = {m.get("media_key"): m for m in pool if isinstance(m, dict)}
        for key in keys:
            m = by_key.get(key)
            if m is None:
                continue
            url = m.get("url") or m.get("preview_image_url")
            kind = "image" if m.get("type") == "photo" else "video"
            if m.get("type") in ("photo", "video", "animated_gif") and isinstance(url, str) and url:
                media.append(MediaRef(kind=kind, url=url))

    return ContentItem(
        platform="x",
        external_id=str(post_id),
        text=text,
        author_external_id=author_id,
        author_handle=handle,
        lang_hint=lang_hint,
        created_at=_parse_time(raw.get("created_at")),
        metrics=metrics,
        parent_external_id=parent,
        quoted_external_id=quoted,
        media=tuple(media),
    )
