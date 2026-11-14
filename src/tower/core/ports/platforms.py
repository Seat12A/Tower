"""Social platforms. ``PlatformClient`` generalises ``XClient`` (kept for compatibility)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from tower.core.domain import error_codes as ec
from tower.core.domain.error_codes import ErrorCode
from tower.core.domain.verification import EmbedPost


class UnsupportedOperation(Exception):
    """The platform does not offer this operation (e.g. no view counts)."""


class PlatformError(Exception):
    """Error of a platform API call. ``code`` is a ``TWR-RAD-*`` catalog entry.

    Never carries a credential: messages and details are safe to log and store.
    """

    default_code: ErrorCode = ec.RAD_BAD_RESPONSE

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code: ErrorCode = self.default_code


class PlatformAuthFailed(PlatformError):
    """401/403: fatal until the credentials are fixed (``TWR-RAD-001``)."""

    default_code = ec.RAD_AUTH_FAILED


class PlatformRateLimited(PlatformError):
    """429. ``retry_after`` is the delay in seconds until the reset announced by the API."""

    default_code = ec.RAD_RATE_LIMITED

    def __init__(self, message: str, *, retry_after: float, status: int | None = 429) -> None:
        super().__init__(message, status=status)
        self.retry_after = retry_after


class PlatformUnavailable(PlatformError):
    """5xx, network error or timeout: retryable (``TWR-RAD-003``)."""

    default_code = ec.RAD_UNAVAILABLE


class PlatformBadResponse(PlatformError):
    """Unparseable or unexpected payload, other 4xx (``TWR-RAD-004``)."""

    default_code = ec.RAD_BAD_RESPONSE


class PlatformNotConfigured(PlatformError):
    """No credentials configured for this platform (``TWR-RAD-007``)."""

    default_code = ec.RAD_NOT_CONFIGURED


@dataclass(frozen=True, slots=True)
class MediaRef:
    kind: str
    """``image``, ``video``, ``audio``, ``document``."""
    url: str


@dataclass(frozen=True, slots=True)
class Metrics:
    values: Mapping[str, int]
    measured_at: datetime


@dataclass(frozen=True, slots=True)
class ContentItem:
    platform: str
    external_id: str
    text: str
    author_external_id: str | None = None
    author_handle: str | None = None
    lang_hint: str | None = None
    created_at: datetime | None = None
    metrics: Metrics | None = None
    parent_external_id: str | None = None
    quoted_external_id: str | None = None
    media: tuple[MediaRef, ...] = ()


@dataclass(frozen=True, slots=True)
class ThreadContext:
    parent: ContentItem | None = None
    quoted: ContentItem | None = None
    ancestors: tuple[ContentItem, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class PublicAccount:
    platform: str
    external_id: str
    handle: str | None = None
    display_name: str | None = None
    description: str | None = None
    verified: bool | None = None
    followers: int | None = None


@dataclass(frozen=True, slots=True)
class ReadUsage:
    """Billable resources returned by one or more API calls (X bills per resource)."""

    posts: int = 0
    users: int = 0

    def __add__(self, other: ReadUsage) -> ReadUsage:
        return ReadUsage(self.posts + other.posts, self.users + other.users)


@dataclass(frozen=True, slots=True)
class PollPage:
    """One page of an account's recent items, newest first."""

    items: tuple[ContentItem, ...]
    newest_id: str | None
    """Newest item id of the page (the next ``since``), None when the page is empty."""
    next_token: str | None
    """Token of the next (older) page within the same ``since`` window."""
    usage: ReadUsage


class PlatformClient(Protocol):
    """Official API of one platform. Unsupported methods raise ``UnsupportedOperation``."""

    platform: str
    cost_per_read_usd_micros: int

    async def get_item(self, external_id: str) -> ContentItem | None: ...

    async def get_thread_context(self, item: ContentItem) -> ThreadContext: ...

    async def get_metrics(self, external_id: str) -> Metrics | None: ...

    async def get_account(self, account_ref: str) -> PublicAccount | None: ...

    async def poll_account(
        self, account_ref: str, *, since: str | None
    ) -> Sequence[ContentItem]: ...


class PollingPlatformClient(PlatformClient, Protocol):
    """Platform client usable by the radar: paged polling and per-resource read prices.

    Cost of a call = ``usage.posts * cost_per_read_usd_micros``
    + ``usage.users * cost_per_account_read_usd_micros``.
    """

    cost_per_account_read_usd_micros: int

    async def poll_page(
        self,
        account_ref: str,
        *,
        since: str | None,
        max_results: int,
        pagination_token: str | None = None,
        exclude: Sequence[str] = (),
    ) -> PollPage: ...


class PublicEmbedClient(Protocol):
    """Public embed of a post (X oEmbed): no credentials, no API quota.

    Used only to check what the extension declares (``core/domain/verification.py``).
    ``lookup`` returns None when the post has no public embed (missing, deleted,
    protected); raises ``PlatformRateLimited``, ``PlatformUnavailable`` or
    ``PlatformBadResponse``.
    """

    async def lookup(self, external_id: str) -> EmbedPost | None: ...
