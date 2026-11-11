"""HTML to plain text (stdlib ``html.parser``): visible text, title, description, date.

The input is capped at 2 MB and the output at 200 000 characters. Nothing of the HTML is
kept: the caller keeps only the returned :class:`ExtractedText`.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser

__all__ = ["MAX_INPUT_CHARS", "MAX_OUTPUT_CHARS", "ExtractedText", "extract_text"]

MAX_INPUT_CHARS = 2_000_000
MAX_OUTPUT_CHARS = 200_000

_DROPPED = frozenset({"script", "style", "noscript", "nav", "header", "footer", "aside", "form"})
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
)
_BLOCKS = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "main",
        "ul",
        "ol",
        "li",
        "table",
        "tr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "pre",
        "br",
        "hr",
        "dl",
        "dt",
        "dd",
        "figure",
        "figcaption",
    }
)
_DATE_META = (
    "article:published_time",
    "og:article:published_time",
    "datepublished",
    "date",
    "dc.date",
    "dcterms.date",
    "pubdate",
    "publishdate",
)
_SPACES = re.compile(r"[ \t\f\r ]+")
_BLANK_LINES = re.compile(r"\n[ \t]*(?:\n[ \t]*)+")


@dataclass(frozen=True, slots=True)
class ExtractedText:
    text: str
    title: str | None = None
    description: str | None = None
    published_at: datetime | None = None


def _parse_date(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class _Extractor(HTMLParser):
    def __init__(self, skip_classes: Collection[str]) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_classes = frozenset(c.lower() for c in skip_classes)
        self._parts: list[str] = []
        self._size = 0
        self._skip_tag: str | None = None
        self._skip_depth = 0
        self._in_title = False
        self.title_parts: list[str] = []
        self.description: str | None = None
        self.published: datetime | None = None
        self.og_title: str | None = None

    @property
    def full(self) -> bool:
        return self._size >= MAX_OUTPUT_CHARS * 2

    def _is_skipped_element(self, tag: str, attrs: dict[str, str | None]) -> bool:
        if tag in _DROPPED:
            return True
        if self._skip_classes and tag not in _VOID:
            classes = (attrs.get("class") or "").lower().split()
            return any(c in self._skip_classes for c in classes)
        return False

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = dict(attrs_list)
        if tag == "meta":
            self._meta(attrs)
            return
        if self._skip_tag is not None:
            if tag == self._skip_tag:
                self._skip_depth += 1
            return
        if self._is_skipped_element(tag, attrs):
            self._skip_tag, self._skip_depth = tag, 1
            return
        if tag == "title":
            self._in_title = True
        if tag in _BLOCKS:
            self._add("\n\n" if tag not in ("br", "li", "tr", "dt", "dd") else "\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if self._skip_tag is not None:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth <= 0:
                    self._skip_tag = None
            return
        if tag == "title":
            self._in_title = False
        if tag in _BLOCKS and tag not in _VOID:
            self._add("\n\n" if tag not in ("li", "tr", "dt", "dd") else "\n")

    def handle_data(self, data: str) -> None:
        if self._skip_tag is not None:
            return
        if self._in_title:
            self.title_parts.append(data)
            return
        self._add(data)

    def _add(self, chunk: str) -> None:
        if self.full:
            return
        self._parts.append(chunk)
        self._size += len(chunk)

    def _meta(self, attrs: dict[str, str | None]) -> None:
        key = (attrs.get("name") or attrs.get("property") or attrs.get("itemprop") or "").lower()
        content = attrs.get("content") or ""
        if not key or not content:
            return
        if key in ("description", "og:description") and self.description is None:
            self.description = content.strip()
        elif key == "og:title" and self.og_title is None:
            self.og_title = content.strip()
        elif key in _DATE_META and self.published is None:
            self.published = _parse_date(content)

    def text(self) -> str:
        raw = "".join(self._parts)
        raw = _SPACES.sub(" ", raw)
        raw = "\n".join(line.strip() for line in raw.split("\n"))
        raw = _BLANK_LINES.sub("\n\n", raw).strip()
        return raw[:MAX_OUTPUT_CHARS]


def extract_text(html: str, *, skip_classes: Collection[str] = ()) -> ExtractedText:
    """Visible text of ``html`` with paragraph breaks.

    ``skip_classes``: extra CSS classes whose elements are dropped (reference lists,
    navigation boxes…).
    """
    parser = _Extractor(skip_classes)
    parser.feed(html[:MAX_INPUT_CHARS])
    parser.close()
    title = " ".join("".join(parser.title_parts).split()) or parser.og_title or None
    return ExtractedText(
        text=parser.text(),
        title=title,
        description=parser.description or None,
        published_at=parser.published,
    )
