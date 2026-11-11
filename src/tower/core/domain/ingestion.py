"""Ingestion value objects and validation rules. Standard library only."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from tower.core.domain import error_codes as ec
from tower.core.domain.errors import InvalidInput
from tower.core.domain.platforms import parse_platform, validate_external_id

MAX_TEXT_CHARS = 20_000
MAX_MEDIA = 10
MAX_URL = 2048
MAX_KEYWORDS = 500
MAX_KEYWORD_CHARS = 100
MAX_FILTER_ACCOUNTS = 2000
MEDIA_KINDS = frozenset({"image", "video", "audio", "document"})

OBSERVED_MAX_AGE = timedelta(hours=24)
OBSERVED_MAX_AHEAD = timedelta(minutes=5)

_FILTER_KEYS = frozenset({"keywords", "accounts", "thresholds"})


@dataclass(frozen=True, slots=True)
class AuthorInput:
    external_id: str | None = None
    handle: str | None = None


@dataclass(frozen=True, slots=True)
class MediaInput:
    kind: str
    url: str


@dataclass(frozen=True, slots=True)
class ItemInput:
    platform: str
    external_id: str
    text: str
    author: AuthorInput | None = None
    lang_hint: str | None = None
    created_at: datetime | None = None
    metrics: Mapping[str, int] = field(default_factory=dict)
    parent_external_id: str | None = None
    quoted_external_id: str | None = None
    media: tuple[MediaInput, ...] = ()
    observed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class IngestResult:
    accepted: int
    new: int
    duplicates: int
    rejected: int
    errors: tuple[tuple[int, str], ...] = ()


def as_utc(value: datetime) -> datetime:
    """Naive datetimes are interpreted as UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def clamp_observed_at(value: datetime | None, now: datetime) -> datetime:
    """Accept ``value`` within [now - 24h, now + 5min], else return ``now``."""
    now = as_utc(now)
    if value is None:
        return now
    value = as_utc(value)
    if now - OBSERVED_MAX_AGE <= value <= now + OBSERVED_MAX_AHEAD:
        return value
    return now


def validate_media(media: MediaInput) -> MediaInput:
    """Kind in image/video/audio/document, http(s) URL of at most 2048 characters."""
    url = media.url
    if media.kind not in MEDIA_KINDS:
        raise InvalidInput("Invalid media kind", kind=str(media.kind)[:32])
    if (
        not isinstance(url, str)
        or not 1 <= len(url) <= MAX_URL
        or not url.isprintable()
        or not url.lower().startswith(("http://", "https://"))
        or len(url.split("://", 1)[1]) == 0
    ):
        raise InvalidInput("Invalid media URL")
    return media


def _bad(message: str, **details: object) -> InvalidInput:
    return InvalidInput(message, code=ec.ING_BAD_FILTERS, **details)


def validate_filters(filters: object) -> dict[str, Any]:
    """Validate ``{"keywords", "accounts", "thresholds"}`` and return a normalized copy."""
    if not isinstance(filters, dict):
        raise _bad("Filters must be an object")
    unknown = sorted(str(k) for k in filters if k not in _FILTER_KEYS)
    if unknown:
        raise _bad("Unknown filter keys", keys=unknown)

    keywords = filters.get("keywords", [])
    if not isinstance(keywords, list) or len(keywords) > MAX_KEYWORDS:
        raise _bad("keywords must be a list of at most 500 strings")
    for kw in keywords:
        if not isinstance(kw, str) or not 1 <= len(kw) <= MAX_KEYWORD_CHARS:
            raise _bad("Each keyword must be a string of 1 to 100 characters")

    accounts = filters.get("accounts", [])
    if not isinstance(accounts, list) or len(accounts) > MAX_FILTER_ACCOUNTS:
        raise _bad("accounts must be a list of at most 2000 entries")
    clean_accounts: list[dict[str, str]] = []
    for acc in accounts:
        if (
            not isinstance(acc, dict)
            or set(acc) != {"platform", "external_id"}
            or not isinstance(acc["platform"], str)
            or not isinstance(acc["external_id"], str)
        ):
            raise _bad("Each account must be {platform, external_id} strings")
        platform = parse_platform(acc["platform"])
        clean_accounts.append(
            {
                "platform": platform.value,
                "external_id": validate_external_id(platform, acc["external_id"]),
            }
        )

    thresholds = filters.get("thresholds", {})
    if not isinstance(thresholds, dict):
        raise _bad("thresholds must be an object of numbers")
    for name, value in thresholds.items():
        if (
            not isinstance(name, str)
            or not name
            or isinstance(value, bool)
            or not isinstance(value, int | float)
            or (isinstance(value, float) and not math.isfinite(value))
        ):
            raise _bad("thresholds must map names to finite numbers", name=str(name)[:64])

    return {
        "keywords": list(keywords),
        "accounts": clean_accounts,
        "thresholds": dict(thresholds),
    }
