"""Verification of contents declared by the extension against the platform (pure).

The extension is an untrusted client: the text and author it declares for a post id are
only claims. A content is trusted once its text comes from a platform read (radar,
manual request, feed: ``platform``) or once the public embed of the post (X oEmbed)
matches it (``verified``). See docs/ingestion.md, "Vérification des contenus".
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from html.parser import HTMLParser


class Verification(StrEnum):
    UNVERIFIED = "unverified"
    """Declared by the extension, not (yet) checked, or check inconclusive."""
    PLATFORM = "platform"
    """Text and author read from the platform API by a service (radar, manual, feed)."""
    VERIFIED = "verified"
    """Declared by the extension and confirmed by the public embed."""
    MISMATCH = "mismatch"
    """The public embed contradicts the declared text or author."""
    UNAVAILABLE = "unavailable"
    """No public embed: post missing, deleted or protected."""


VERIFICATION_VALUES = tuple(v.value for v in Verification)
TRUSTED = frozenset({Verification.PLATFORM.value, Verification.VERIFIED.value})
"""States whose text may trigger a targeted (N3/N4) extraction."""
CORRECTABLE = frozenset(
    {Verification.UNVERIFIED.value, Verification.MISMATCH.value, Verification.UNAVAILABLE.value}
)
"""States replaced by a platform read, which then announces ``content.verified``."""

INCONCLUSIVE_PREFIX_CHARS = 250
"""An embed text that is a strict prefix of the declared text, at least this long, may
be a truncated long post: inconclusive, never ``verified``."""

_URL = re.compile(
    r"(?:https?://\S+"
    r"|\b(?:pic\.(?:twitter|x)\.com|t\.co)/\S+"
    r"|\S+\.\S+/\S*(?:…|\.\.\.)"
    r"|\S+\.\S+(?:…|\.\.\.))",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class EmbedPost:
    """What the public embed tells about a post."""

    text: str
    author_handle: str | None


class _ParagraphText(HTMLParser):
    """Text of the first ``<p>`` of the embed blockquote; ``<br>`` becomes a newline."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.done = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.done:
            return
        if tag == "p":
            self.depth += 1
        elif tag == "br" and self.depth:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "p" and self.depth:
            self.depth -= 1
            if self.depth == 0:
                self.done = True

    def handle_data(self, data: str) -> None:
        if self.depth and not self.done:
            self.parts.append(data)


def embed_text(html: str) -> str | None:
    """Post text from the oEmbed ``html`` field, or None without a paragraph."""
    parser = _ParagraphText()
    parser.feed(html)
    parser.close()
    if not parser.parts and not parser.done:
        return None
    return "".join(parser.parts)


def handle_from_author_url(url: str | None) -> str | None:
    """``https://x.com/Name`` → ``name`` (lower case); None if not a profile URL."""
    if not url:
        return None
    match = re.fullmatch(r"https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/?", url)
    return match.group(1).lower() if match else None


_WS = re.compile(r"\s+")
_DROPPED_CATEGORIES = frozenset({"So", "Sk", "Cf", "Mn"})
"""Pictographs (the page may render emoji as images), format characters (ZWJ) and
combining marks (variation selectors)."""


def comparable(text: str) -> str:
    """Text reduced for comparison: NFKC, lower case, links removed (the embed shows
    ``t.co`` links, the page shows shortened display URLs), pictographs removed,
    whitespace compacted. Unlike the E0 normalisation, mentions are kept: a changed
    mention is a different post."""
    text = _URL.sub(" ", unicodedata.normalize("NFKC", _URL.sub(" ", text)).lower())
    if not text.isascii():
        text = "".join(ch for ch in text if unicodedata.category(ch) not in _DROPPED_CATEGORIES)
    return _WS.sub(" ", text).strip()


def compare(
    declared_text: str, declared_handle: str | None, embed: EmbedPost | None
) -> Verification:
    """Outcome of the check of a declared content against its public embed.

    ``UNVERIFIED`` means inconclusive (possible truncation of a long post): the content
    keeps its state and is not checked again.
    """
    if embed is None:
        return Verification.UNAVAILABLE
    if (
        declared_handle
        and embed.author_handle
        and declared_handle.strip().lstrip("@").lower() != embed.author_handle.lower()
    ):
        return Verification.MISMATCH
    declared = comparable(declared_text)
    shown = comparable(embed.text)
    if declared == shown:
        return Verification.VERIFIED
    if (
        len(shown) >= INCONCLUSIVE_PREFIX_CHARS
        and len(declared) > len(shown)
        and declared.startswith(shown.rstrip(".…").rstrip())
    ):
        return Verification.UNVERIFIED
    return Verification.MISMATCH
