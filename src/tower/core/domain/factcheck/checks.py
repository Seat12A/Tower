"""Deterministic checks of E9 (pipeline.md): quotes, note length, loaded lexicon, links,
dates. No model involved; a failed check rejects or downgrades the draft, or warns the
reviewer, as documented on each function."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from tower.core.domain.factcheck.datamark import MARKER

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
    text = text.replace(MARKER, " ").translate(_CHAR_MAP)
    text = unicodedata.normalize("NFKC", text).translate(_CHAR_MAP)
    return _SPACES.sub(" ", text).strip()


def quote_in_passage(quote: str, original_passage: str) -> bool:
    """True when the normalized quote (≥ 1 non-space char) occurs in the normalized
    **original** passage text (never a translation). An ellipsis ``...`` inside the quote
    splits it: every part must occur, in order."""
    parts = [
        _squash_quote_spaces(p)
        for p in _ELLIPSIS_SPLIT.split(normalize_for_quote(quote))
        if p.strip()
    ]
    if not parts:
        return False
    haystack = _squash_quote_spaces(normalize_for_quote(original_passage))
    pos = 0
    for part in parts:
        found = haystack.find(part, pos)
        if found < 0:
            return False
        pos = found + len(part)
    return True


def note_length(text: str) -> int:
    """Characters of a note, URLs (``http(s)://…``) excluded, surrounding spaces stripped."""
    return len(_SPACES.sub(" ", _URL.sub(" ", text)).strip())


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
    folded_text = _fold(text)
    allowed = allowed or {}
    allowed_spans: list[tuple[int, int]] = []
    for key in _lang_keys(lang):
        for term in allowed.get(key, ()):
            allowed_spans.extend(
                m.span() for m in _term_regex(term, folded=True).finditer(folded_text)
            )
    hits: list[LoadedTermHit] = []
    seen: set[str] = set()
    for key in _lang_keys(lang):
        for term in lexicon.get(key, ()):
            if term in seen or not _fold(term).strip():
                continue
            for m in _term_regex(term, folded=True).finditer(folded_text):
                start, end = m.span()
                if any(a <= start and end <= b for a, b in allowed_spans):
                    continue
                seen.add(term)
                hits.append(LoadedTermHit(term=term, lang=key))
                break
    return hits


def check_links(texts: Iterable[str], retrieved_urls: Iterable[str]) -> list[str]:
    """URLs present in ``texts`` that are not retrieved sources. Comparison ignores the
    scheme, a leading ``www.``, a trailing slash and the fragment."""
    known = {_url_key(u) for u in retrieved_urls}
    unknown: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for match in _LINK.finditer(text):
            url = match.group(0).rstrip(_URL_TRAILING)
            key = _url_key(url)
            if key in known or key in seen:
                continue
            seen.add(key)
            unknown.append(url)
    return unknown


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
    reference = _aware(post_date) if post_date is not None else datetime.now(UTC)
    after_post: list[str] = []
    too_old: list[str] = []
    for source in sources:
        if source.published_at is None:
            continue
        published = _aware(source.published_at)
        if post_date is not None and published > reference:
            after_post.append(source.key)
        elif max_age is not None and published < reference - max_age:
            too_old.append(source.key)
    return {"after_post": after_post, "too_old": too_old}


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
        return {
            "invalid_quotes": list(self.invalid_quotes),
            "note_too_long": dict(self.note_too_long),
            "loaded_terms": [{"term": h.term, "lang": h.lang} for h in self.loaded_terms],
            "unknown_links": list(self.unknown_links),
            "sources_after_post": list(self.sources_after_post),
            "sources_too_old": list(self.sources_too_old),
            "downgraded": self.downgraded,
            "blocking": self.blocking,
        }


_CHAR_MAP = str.maketrans(
    {
        "\u00ab": '"',  # «
        "\u00bb": '"',  # »
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
        "\u2039": '"',
        "\u203a": '"',
        "\u2033": '"',  # double prime
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u2032": "'",  # prime
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2026": "...",
    }
)
_SPACES = re.compile(r"\s+")
_ELLIPSIS_SPLIT = re.compile(r"\[\.\.\.\]|\(\.\.\.\)|\.\.\.")
_QUOTE_SPACES = re.compile(r'\s*"\s*')
_URL = re.compile(r"https?://\S+")
_LINK = re.compile(r"https?://[^\s<>\"]+")
_URL_TRAILING = ".,;:!?)]}'\"\u00bb\u201d"


def _squash_quote_spaces(text: str) -> str:
    """Remove spaces adjacent to ``"``: French typography puts (narrow) spaces inside
    « », so ``« oui »`` and ``"oui"`` must compare equal. Applied on both sides."""
    return _QUOTE_SPACES.sub('"', text).strip()


def _fold(text: str) -> str:
    """Casefolded and accent-free (NFD, combining marks dropped)."""
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _lang_keys(lang: str) -> list[str]:
    return [lang] if lang == "*" else [lang, "*"]


def _term_regex(term: str, *, folded: bool) -> re.Pattern[str]:
    """Whole-word pattern of a term. A boundary is only required on a side where the term
    starts or ends with a word character, so non-word terms (``!!``, ``?!``) match as
    plain substrings."""
    words = (_fold(term) if folded else term).split()
    body = r"\s+".join(re.escape(w) for w in words)
    first, last = words[0][0], words[-1][-1]
    left = r"(?<!\w)" if re.match(r"\w", first) else ""
    right = r"(?!\w)" if re.match(r"\w", last) else ""
    return re.compile(left + body + right)


def _url_key(url: str) -> str:
    key = url.strip().split("#", 1)[0]
    key = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", key)
    host, sep, rest = key.partition("/")
    host = host.lower().removeprefix("www.")
    return (host + sep + rest).rstrip("/")


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value
