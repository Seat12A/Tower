from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SearchResult:
    url: str
    title: str
    snippet: str
    published_at: datetime | None = None
    lang: str | None = None


class SearchClient(Protocol):
    """Web search (Brave…). Contract only."""

    async def search(
        self,
        query: str,
        *,
        limit: int,
        lang: str | None = None,
        country: str | None = None,
        include_domains: Sequence[str] = (),
        exclude_domains: Sequence[str] = (),
    ) -> Sequence[SearchResult]: ...


@dataclass(frozen=True, slots=True)
class FetchedPage:
    url: str
    final_url: str
    status: int
    content_type: str
    text: str
    """Decoded text (HTML, text, JSON, XML); empty for binary types such as PDF."""
    sha256: str
    """Hex SHA-256 of the raw body."""
    fetched_at: datetime
    body: bytes = b""
    """Raw bytes, only kept for binary types (PDF)."""


class WebFetcher(Protocol):
    """Safe URL fetch (SSRF protection, size cap, robots.txt)."""

    async def fetch(self, url: str) -> FetchedPage: ...
