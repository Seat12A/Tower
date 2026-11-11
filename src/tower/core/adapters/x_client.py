"""X (Twitter) official API v2 adapter: implements ``PollingPlatformClient``.

Names checked against the official OpenAPI spec (https://api.x.com/2/openapi.json,
version 2.174, 2026-11-11) and docs.x.com: see docs/radar.md, "Vérifications".
httpx is imported lazily (in ``__init__``): importing this module stays cheap.
The bearer token is never logged nor put in an error.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from tower.core.ports.platforms import (
    ContentItem,
    Metrics,
    PollPage,
    PublicAccount,
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
        raise NotImplementedError

    cost_per_read_usd_micros: int
    cost_per_account_read_usd_micros: int

    async def aclose(self) -> None:
        """Close the httpx client if this instance created it."""
        raise NotImplementedError

    async def get_account(self, account_ref: str) -> PublicAccount | None:
        """``GET /2/users/{id}`` if ``account_ref`` is numeric, else
        ``GET /2/users/by/username/{username}`` (leading ``@`` stripped). 404 or a
        ``Not Found`` problem in ``errors`` without ``data`` -> None."""
        raise NotImplementedError

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
        raise NotImplementedError

    async def poll_account(self, account_ref: str, *, since: str | None) -> Sequence[ContentItem]:
        """All pages since ``since`` (at most 3 pages of 100), newest first."""
        raise NotImplementedError

    async def get_item(self, external_id: str) -> ContentItem | None:
        """``GET /2/tweets/{id}``; 404 -> None."""
        raise NotImplementedError

    async def get_metrics(self, external_id: str) -> Metrics | None:
        """Public metrics of one post (same call as ``get_item``)."""
        raise NotImplementedError

    async def get_thread_context(self, item: ContentItem) -> ThreadContext:
        """Parent and quoted posts in ONE ``GET /2/tweets?ids=a,b`` call (no call when
        the item references nothing)."""
        raise NotImplementedError


def parse_post(raw: dict[str, Any], includes: dict[str, Any]) -> ContentItem:
    """Convert one API post (+ the response ``includes``) into a ``ContentItem``."""
    raise NotImplementedError
