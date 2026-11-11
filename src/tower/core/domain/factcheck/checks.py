"""Deterministic checks of E9 (pipeline.md): quotes, note length, loaded lexicon, links,
dates. No model involved; a failed check rejects or downgrades the draft, or warns the
reviewer, as documented on each function."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

__all__ = [
    "NOTE_MAX_CHARS",
    "CheckReport",
    "DatedSource",
    "LoadedTermHit",
    "check_dates",
    "check_links",
    "find_loaded_terms",
    "normalize_for_quote",
    "note_length",
    "quote_in_passage",
]

NOTE_MAX_CHARS = 280
"""Context note limit, links excluded."""


def normalize_for_quote(text: str) -> str:
    """Comparable form: NFKC, ``datamark.MARKER`` → space, typographic quotes and
    apostrophes (« » “ ” „ ‘ ’ ‹ › ″ ′) → ASCII ``"`` and ``'``, dashes (– — ‑) → ``-``,
    ellipsis character → ``...``, every whitespace run (incl. non-breaking) → one space,
    stripped. Case is kept."""
    raise NotImplementedError


def quote_in_passage(quote: str, original_passage: str) -> bool:
    """True when the normalized quote (≥ 1 non-space char) occurs in the normalized
    **original** passage text (never a translation). An ellipsis ``...`` inside the quote
    splits it: every part must occur, in order."""
    raise NotImplementedError


def note_length(text: str) -> int:
    """Characters of a note, URLs (``http(s)://…``) excluded, surrounding spaces stripped."""
    raise NotImplementedError


@dataclass(frozen=True, slots=True)
class LoadedTermHit:
    term: str
    lang: str


def find_loaded_terms(
    text: str,
    lang: str,
    lexicon: Mapping[str, Sequence[str]],
    *,
    allowed: Mapping[str, Sequence[str]] | None = None,
) -> list[LoadedTermHit]:
    """Loaded terms of ``lexicon[lang]`` (and ``lexicon["*"]``) found in ``text``,
    whole-word, casefolded and accent-insensitive; multi-word terms allowed. Terms of
    ``allowed[lang]`` (established legal or historical qualifications) are never
    reported, even when they contain a loaded term."""
    raise NotImplementedError


def check_links(texts: Iterable[str], retrieved_urls: Iterable[str]) -> list[str]:
    """URLs present in ``texts`` that are not retrieved sources. Comparison ignores the
    scheme, a leading ``www.``, a trailing slash and the fragment."""
    raise NotImplementedError


@dataclass(frozen=True, slots=True)
class DatedSource:
    key: str
    published_at: datetime | None


def check_dates(
    sources: Sequence[DatedSource], *, post_date: datetime | None, max_age: timedelta | None
) -> dict[str, list[str]]:
    """``{"after_post": [...], "too_old": [...]}`` source keys: published after the post
    (when both dates are known), or older than ``max_age`` relative to the post date (or
    now in UTC when the post date is unknown). Undated sources are never reported."""
    raise NotImplementedError


@dataclass(slots=True)
class CheckReport:
    """Results stored with the context note (``context_notes.checks``)."""

    invalid_quotes: list[str] = field(default_factory=list)
    """Passage ids whose quote was not found verbatim."""
    note_too_long: dict[str, int] = field(default_factory=dict)
    """Language → length, for notes over ``NOTE_MAX_CHARS``."""
    loaded_terms: list[LoadedTermHit] = field(default_factory=list)
    unknown_links: list[str] = field(default_factory=list)
    sources_after_post: list[str] = field(default_factory=list)
    sources_too_old: list[str] = field(default_factory=list)
    downgraded: bool = False
    """``TRUE``/``FALSE`` downgraded by the independence rule."""

    @property
    def blocking(self) -> bool:
        """Invalid quotes, unknown links or a note too long reject the draft text."""
        return bool(self.invalid_quotes or self.unknown_links or self.note_too_long)

    def as_dict(self) -> dict[str, object]:
        raise NotImplementedError
