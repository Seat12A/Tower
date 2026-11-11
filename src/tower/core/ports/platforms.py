"""Social platforms. ``PlatformClient`` generalises ``XClient`` (kept for compatibility)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


class UnsupportedOperation(Exception):
    """The platform does not offer this operation (e.g. no view counts)."""


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
