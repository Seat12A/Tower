"""Manual notes: a verdict and context note written by the team, without any model.

Validation only (standard library). The draft goes through the usual review
(``POST /v1/drafts/{id}/review``): writing a note never publishes it by itself.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from tower.core.domain import error_codes as ec
from tower.core.domain.errors import InvalidInput
from tower.core.domain.factcheck.checks import NOTE_MAX_CHARS, note_length
from tower.core.domain.factcheck.values import ClaimType, ContextNeed, ContextType, Veracity

__all__ = [
    "CLAIM_MAX_CHARS",
    "FACT_SHEET_MAX_CHARS",
    "MANUAL_PROMPT_VERSION",
    "MAX_LINKS",
    "NOTE_LANGS",
    "ManualNote",
    "default_context_need",
    "validate_manual_note",
]

CLAIM_MAX_CHARS = 300
"""Same limit as ``claims.text_fr`` (CHECK)."""
FACT_SHEET_MAX_CHARS = 4000
MAX_LINKS = 10
MAX_LINK_CHARS = 2048
NOTE_LANGS = ("fr", "en")
MANUAL_PROMPT_VERSION = "manual"
"""``claims.prompt_version`` of a claim written by the team."""

_URL = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ManualNote:
    """A validated manual note. ``claim_text`` is in French (``claims.text_fr``)."""

    claim_text: str
    claim_type: ClaimType
    veracity: Veracity
    context_need: ContextNeed
    context_types: tuple[ContextType, ...]
    note_text: str | None
    note_lang: str
    links: tuple[str, ...]
    fact_sheet: str | None

    def to_payload(self) -> dict[str, Any]:
        """JSON form (job payload); ``validate_manual_note(**payload)`` reads it back."""
        data = asdict(self)
        data["context_types"] = list(data["context_types"])
        data["links"] = list(data["links"])
        return data

    def key(self, platform: str, external_id: str, author: str) -> str:
        """Idempotency key: same author, same post, same content → same draft."""
        canonical = json.dumps(
            {"p": platform, "e": external_id, "a": author, "n": self.to_payload()},
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def _invalid(message: str, **details: object) -> InvalidInput:
    return InvalidInput(message, code=ec.FCK_MANUAL_INVALID, **details)


def default_context_need(veracity: Veracity) -> ContextNeed:
    """``necessary`` for misleading or false, ``useful`` for mostly true or unverifiable,
    ``none`` for true."""
    if veracity in (Veracity.MISLEADING, Veracity.FALSE):
        return ContextNeed.NECESSARY
    if veracity is Veracity.TRUE:
        return ContextNeed.NONE
    return ContextNeed.USEFUL


def _enum[E: (ClaimType, Veracity, ContextNeed, ContextType)](
    kind: type[E], value: str, field: str
) -> E:
    try:
        return kind(value.strip().lower())
    except ValueError:
        raise _invalid(
            f"Unknown {field}", field=field, value=value[:40], allowed=[v.value for v in kind]
        ) from None


def _text(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None


def validate_manual_note(
    *,
    claim_text: str,
    claim_type: str,
    veracity: str,
    context_need: str | None = None,
    context_types: Sequence[str] = (),
    note_text: str | None = None,
    note_lang: str = "fr",
    links: Sequence[str] = (),
    fact_sheet: str | None = None,
) -> ManualNote:
    """Check every field; raise ``InvalidInput`` (``TWR-FCK-049``) naming the field."""
    claim = _text(claim_text)
    if claim is None:
        raise _invalid("The claim is required", field="claim_text")
    if len(claim) > CLAIM_MAX_CHARS:
        raise _invalid("The claim is too long", field="claim_text", max_chars=CLAIM_MAX_CHARS)
    ctype = _enum(ClaimType, claim_type, "claim_type")
    outcome = _enum(Veracity, veracity, "veracity")
    need_text = _text(context_need)
    need = (
        _enum(ContextNeed, need_text, "context_need")
        if need_text is not None
        else default_context_need(outcome)
    )
    types: list[ContextType] = []
    for raw in context_types:
        if not raw.strip():
            continue
        value = _enum(ContextType, raw, "context_types")
        if value not in types:
            types.append(value)
    lang = (note_lang or "fr").strip().lower()
    if lang not in NOTE_LANGS:
        raise _invalid("The note language must be fr or en", field="note_lang")
    note = _text(note_text)
    if note is not None and note_length(note) > NOTE_MAX_CHARS:
        raise _invalid(
            "The note is too long (links excluded)", field="note_text", max_chars=NOTE_MAX_CHARS
        )
    sheet = _text(fact_sheet)
    if sheet is not None and len(sheet) > FACT_SHEET_MAX_CHARS:
        raise _invalid(
            "The fact sheet is too long", field="fact_sheet", max_chars=FACT_SHEET_MAX_CHARS
        )
    if sheet is not None and note is None:
        raise _invalid("A fact sheet needs a note", field="fact_sheet")
    clean_links: list[str] = []
    for raw in links:
        url = raw.strip()
        if not url:
            continue
        if len(url) > MAX_LINK_CHARS or not _URL.match(url):
            raise _invalid("Invalid link (http or https URL expected)", field="links")
        if url not in clean_links:
            clean_links.append(url)
    if len(clean_links) > MAX_LINKS:
        raise _invalid("Too many links", field="links", max_links=MAX_LINKS)
    if clean_links and note is None:
        raise _invalid("Links need a note", field="links")
    if need is not ContextNeed.NONE and note is None and outcome is not Veracity.UNVERIFIABLE:
        raise _invalid("A note is required when context is useful or necessary", field="note_text")
    return ManualNote(
        claim_text=claim,
        claim_type=ctype,
        veracity=outcome,
        context_need=need,
        context_types=tuple(types),
        note_text=note,
        note_lang=lang,
        links=tuple(clean_links),
        fact_sheet=sheet,
    )
