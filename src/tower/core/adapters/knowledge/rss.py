"""RSS 2.0, RSS 1.0 (RDF) and Atom parsing plus a conditional-GET fetch.

Only the headline data is kept: title, link, lead (HTML stripped, whitespace collapsed,
at most 600 characters), publication date and guid. Never the full article body.
Every document goes through :mod:`safe_xml` (no entity declaration, size and depth caps);
a rejected document raises :class:`UnsafeXml` (mapped to ``TWR-ATL-010`` by the caller).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Protocol

from tower.core.adapters.knowledge.html_text import extract_text
from tower.core.adapters.knowledge.safe_xml import StreamingXml, UnsafeXml, text_of

if TYPE_CHECKING:
    import httpx

__all__ = [
    "LEAD_MAX_CHARS",
    "MAX_FEED_BYTES",
    "FeedEntry",
    "FetchResult",
    "UnsafeXml",
    "fetch_feed",
    "parse_feed",
]

LEAD_MAX_CHARS = 600
MAX_FEED_BYTES = 2_000_000
_LEAD_FIELDS = ("description", "summary", "content", "encoded")
_DATE_FIELDS = ("pubDate", "published", "date", "updated", "issued", "created")
_TAGS = re.compile(r"<[^>]*>")


@dataclass(frozen=True, slots=True)
class FeedEntry:
    title: str
    url: str
    lead: str
    published: datetime | None
    guid: str | None


@dataclass(frozen=True, slots=True)
class FetchResult:
    status: int
    entries: list[FeedEntry]
    etag: str | None = None
    last_modified: str | None = None

    @property
    def not_modified(self) -> bool:
        return self.status == 304


class FeedLike(Protocol):
    url: str


def _clean_text(raw: str, *, limit: int) -> str:
    if "<" in raw:
        raw = extract_text(raw).text or _TAGS.sub(" ", raw)
    return " ".join(raw.split())[:limit].strip()


def _parse_date(raw: str) -> datetime | None:
    raw = raw.strip()
    if not raw:
        return None
    parsed: datetime | None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _first_text(entry: ET.Element, names: tuple[str, ...]) -> str:
    for name in names:
        for child in entry:
            if child.tag == name:
                value = text_of(child) if len(child) == 0 else "".join(child.itertext())
                if value.strip():
                    return value
    return ""


def _link(entry: ET.Element) -> str:
    atom_href = ""
    for child in entry:
        if child.tag != "link":
            continue
        href = child.attrib.get("href")
        if href is None:
            if text_of(child):
                return text_of(child)
        elif child.attrib.get("rel") in (None, "", "alternate") and not atom_href:
            atom_href = href.strip()
    return atom_href


def _entry(element: ET.Element) -> FeedEntry | None:
    title = _clean_text(_first_text(element, ("title",)), limit=500)
    url = _link(element)
    if not title or not url.startswith(("http://", "https://")):
        return None
    published: datetime | None = None
    for name in _DATE_FIELDS:
        value = _first_text(element, (name,))
        if value and (published := _parse_date(value)) is not None:
            break
    guid = _first_text(element, ("guid", "id")).strip() or None
    return FeedEntry(
        title=title,
        url=url,
        lead=_clean_text(_first_text(element, _LEAD_FIELDS), limit=LEAD_MAX_CHARS),
        published=published,
        guid=guid,
    )


def parse_feed(data: bytes, *, max_items: int, max_bytes: int = MAX_FEED_BYTES) -> list[FeedEntry]:
    """Entries of an RSS/RDF/Atom document (at most ``max_items``, document order).

    Entries without a title or an http(s) link are dropped. Raises :class:`UnsafeXml`.
    """
    parser = StreamingXml(select=("item", "entry"), max_bytes=max_bytes)
    entries: list[FeedEntry] = []

    def take(elements: list[ET.Element]) -> None:
        for element in elements:
            if len(entries) < max_items and (entry := _entry(element)) is not None:
                entries.append(entry)

    take(parser.feed(data))
    take(parser.close())
    return entries


class _Http(Protocol):
    async def request(
        self, method: str, url: str, *, headers: dict[str, str] | None = ...
    ) -> httpx.Response: ...


async def fetch_feed(
    http: _Http,
    feed: FeedLike,
    *,
    etag: str | None,
    last_modified: str | None,
    max_items: int = 50,
) -> FetchResult:
    """Conditional GET (``If-None-Match`` / ``If-Modified-Since``); 304 → no entries.

    ``http`` is a :class:`SourceHttp` (HTTP errors raise ``SourceUnavailable``).
    """
    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    response = await http.request("GET", feed.url, headers=headers or None)
    if response.status_code == 304:
        return FetchResult(304, [], etag, last_modified)
    declared = response.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_FEED_BYTES:
        raise UnsafeXml("document too large")
    entries = parse_feed(response.content, max_items=max_items)
    return FetchResult(
        response.status_code,
        entries,
        response.headers.get("etag"),
        response.headers.get("last-modified"),
    )
