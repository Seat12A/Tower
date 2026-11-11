"""Model inputs, one builder per role. These functions are the only place deciding what
a model sees: never the author, their side, engagement metrics, the processing level or
the reviewer (tested for ``frame``, ``judge``, ``second_opinion`` and ``objection``).

Untrusted text (posts, passages) is datamarked; mentions are pseudonymized (``@user``).
The router wraps the whole input in an ``<input>`` block marked as untrusted data.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from tower.core.domain.factcheck.datamark import datamark

__all__ = [
    "FORBIDDEN_INPUT_KEYS",
    "ClaimView",
    "PassageView",
    "SourceView",
    "critic_input",
    "extract_input",
    "find_forbidden_keys",
    "frame_input",
    "judge_input",
    "objection_input",
    "pseudonymize",
    "query_input",
    "second_opinion_input",
    "translate_input",
    "writer_input",
]

FORBIDDEN_INPUT_KEYS = frozenset(
    {
        "author",
        "author_id",
        "author_handle",
        "author_external_id",
        "handle",
        "display_name",
        "followers",
        "metrics",
        "engagement",
        "likes",
        "reposts",
        "impressions",
        "reach",
        "level",
        "priority",
        "political",
        "political_label",
        "party",
        "side",
        "watched",
        "category",
        "reviewer",
    }
)

_MENTION = re.compile(r"(?<![\w@])@\w{1,30}")
CONTEXT_MAX_CHARS = 280


def pseudonymize(text: str) -> str:
    """Every ``@mention`` becomes ``@user``."""
    return _MENTION.sub("@user", text)


def find_forbidden_keys(value: Any, path: str = "") -> list[str]:
    """Paths of forbidden keys anywhere in a nested input (used by the tests and by
    ``LLMRequest`` builders in debug)."""
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, sub in value.items():
            here = f"{path}.{key}" if path else str(key)
            if str(key).lower() in FORBIDDEN_INPUT_KEYS:
                found.append(here)
            found.extend(find_forbidden_keys(sub, here))
    elif isinstance(value, list | tuple):
        for i, sub in enumerate(value):
            found.extend(find_forbidden_keys(sub, f"{path}[{i}]"))
    return found


def _iso(value: date | datetime | None) -> str | None:
    return None if value is None else value.isoformat()


@dataclass(frozen=True, slots=True)
class ClaimView:
    """What the models know about a claim (built from a ``claims`` row)."""

    text_original: str
    lang: str
    text_fr: str
    claim_type: str
    jurisdictions: tuple[str, ...]
    primary_jurisdiction: str | None
    signature: Mapping[str, Any]
    entities: tuple[Mapping[str, Any], ...]
    """``{"name", "role"}`` only."""
    context_hints: tuple[str, ...]
    thread_context: str | None

    def as_input(self) -> dict[str, Any]:
        return {
            "text_original": self.text_original,
            "lang": self.lang,
            "text_fr": self.text_fr,
            "claim_type": self.claim_type,
            "jurisdictions": list(self.jurisdictions),
            "primary_jurisdiction": self.primary_jurisdiction,
            "signature": dict(self.signature),
            "entities": [{"name": e["name"], "role": e["role"]} for e in self.entities],
            "context_hints": list(self.context_hints),
            "thread_context": self.thread_context,
        }


@dataclass(frozen=True, slots=True)
class SourceView:
    id: str
    """``S1``… within the investigation."""
    url: str
    publisher: str | None
    domain: str
    tier: str
    title: str | None
    published_at: datetime | None


@dataclass(frozen=True, slots=True)
class PassageView:
    id: str
    """``P1``…"""
    source: SourceView
    lang: str
    text: str
    """Original text (datamarked when sent)."""
    translation_fr: str | None = None


def _passage(p: PassageView, *, with_url: bool = False) -> dict[str, Any]:
    source: dict[str, Any] = {
        "id": p.source.id,
        "publisher": p.source.publisher,
        "domain": p.source.domain,
        "tier": p.source.tier,
        "title": p.source.title,
        "published_at": _iso(p.source.published_at),
    }
    if with_url:
        source["url"] = p.source.url
    out: dict[str, Any] = {
        "id": p.id,
        "source": source,
        "lang": p.lang,
        "text": datamark(p.text),
    }
    if p.translation_fr:
        out["translation_fr"] = datamark(p.translation_fr)
    return out


def extract_input(
    *,
    post_id: str,
    lang: str,
    text: str,
    created_at: datetime | None,
    today: date,
    context_kind: str | None = None,
    context_text: str | None = None,
) -> dict[str, Any]:
    """E2: one post (``context_kind`` = ``reply`` or ``quote``, text cut to 280 chars)."""
    context = None
    if context_kind and context_text:
        context = {
            "kind": context_kind,
            "text": datamark(pseudonymize(context_text[:CONTEXT_MAX_CHARS])),
        }
    return {
        "today": today.isoformat(),
        "post": {
            "id": post_id,
            "lang": lang,
            "created_at": _iso(created_at),
            "text": datamark(pseudonymize(text)),
            "context": context,
        },
    }


def frame_input(claim: ClaimView, *, today: date, post_date: date | None) -> dict[str, Any]:
    return {
        "today": today.isoformat(),
        "post_date": _iso(post_date),
        "claim": claim.as_input(),
    }


def query_input(
    claim: ClaimView,
    framing: Mapping[str, Any],
    *,
    today: date,
    max_per_side: int,
    source_languages: Sequence[str],
) -> dict[str, Any]:
    return {
        "today": today.isoformat(),
        "claim": claim.as_input(),
        "framing": dict(framing),
        "max_queries_per_side": max_per_side,
        "source_languages": list(source_languages),
    }


def judge_input(
    claim: ClaimView,
    framing: Mapping[str, Any],
    passages: Sequence[PassageView],
    *,
    today: date,
    post_date: date | None,
) -> dict[str, Any]:
    return {
        "today": today.isoformat(),
        "post_date": _iso(post_date),
        "claim": claim.as_input(),
        "framing": dict(framing),
        "passages": [_passage(p) for p in passages],
    }


def second_opinion_input(
    claim: ClaimView,
    framing: Mapping[str, Any],
    passages: Sequence[PassageView],
    *,
    today: date,
    post_date: date | None,
) -> dict[str, Any]:
    """Same evidence as the judge, in reverse order, without the verdict."""
    return judge_input(claim, framing, list(reversed(passages)), today=today, post_date=post_date)


def objection_input(
    claim: ClaimView,
    framing: Mapping[str, Any],
    passages: Sequence[PassageView],
    judge: Mapping[str, Any],
    second: Mapping[str, Any],
    *,
    today: date,
    post_date: date | None,
) -> dict[str, Any]:
    """``judge``: veracity, context_need, rationale. ``second``: veracity, reason."""
    base = judge_input(claim, framing, list(reversed(passages)), today=today, post_date=post_date)
    base["judge_verdict"] = {
        "veracity": judge["veracity"],
        "context_need": judge["context_need"],
        "rationale": judge.get("rationale", []),
    }
    base["independent_verdict"] = {"veracity": second["veracity"], "reason": second["reason"]}
    return base


def writer_input(
    claim: ClaimView,
    verdict: Mapping[str, Any],
    sources: Sequence[SourceView],
    *,
    post_text: str,
    post_lang: str,
    post_date: date | None,
    languages: Sequence[str],
    checked_at: datetime,
) -> dict[str, Any]:
    """``verdict``: veracity, context_need, context (points), rationale (with quotes).
    ``languages``: post language first, then the reference languages."""
    return {
        "checked_at": checked_at.isoformat(timespec="minutes"),
        "post": {
            "lang": post_lang,
            "date": _iso(post_date),
            "text": datamark(pseudonymize(post_text)),
        },
        "claim": claim.as_input(),
        "verdict": dict(verdict),
        "sources": [
            {
                "id": s.id,
                "url": s.url,
                "publisher": s.publisher,
                "title": s.title,
                "published_at": _iso(s.published_at),
            }
            for s in sources
        ],
        "languages": list(languages),
    }


def critic_input(
    claim: ClaimView, verdict: Mapping[str, Any], notes: Sequence[Mapping[str, str]]
) -> dict[str, Any]:
    """``notes``: ``{"lang", "text"}``; ``verdict``: veracity, context_need, rationale."""
    return {
        "claim": {"text_fr": claim.text_fr, "text_original": claim.text_original},
        "verdict": dict(verdict),
        "notes": [{"lang": n["lang"], "text": n["text"]} for n in notes],
    }


def translate_input(*, text: str, source_lang: str, target_lang: str, kind: str) -> dict[str, Any]:
    """``kind``: ``context_note``, ``fact_sheet`` or ``passage``."""
    return {
        "kind": kind,
        "source_lang": source_lang,
        "target_lang": target_lang,
        "text": text,
    }
