"""Platforms, public roles and platform identifiers. Standard library only."""

from __future__ import annotations

from enum import StrEnum

from tower.core.domain import error_codes as ec
from tower.core.domain.errors import InvalidInput


class Platform(StrEnum):
    X = "x"
    BLUESKY = "bluesky"
    THREADS = "threads"
    MASTODON = "mastodon"
    YOUTUBE = "youtube"
    TIKTOK = "tiktok"
    TELEGRAM = "telegram"
    WEB = "web"


class PublicRole(StrEnum):
    """Closed category of public role (never a political side)."""

    HEAD_OF_STATE_OR_GOVERNMENT = "head_of_state_or_government"
    MINISTER = "minister"
    MEMBER_OF_PARLIAMENT = "member_of_parliament"
    ELECTED_OFFICIAL = "elected_official"
    CANDIDATE = "candidate"
    POLITICAL_PARTY = "political_party"
    PUBLIC_INSTITUTION = "public_institution"
    MEDIA_OUTLET = "media_outlet"
    JOURNALIST = "journalist"
    PUBLIC_FIGURE = "public_figure"
    OTHER = "other"


X_MAX_ID = 9_223_372_036_854_775_807
"""Upper bound of PostgreSQL BIGINT: X identifiers are stored as BIGINT."""
X_ID_PATTERN = "^[0-9]{1,19}$"
"""Also used by the CHECK constraints of the tables (with the BIGINT bound)."""
MAX_EXTERNAL_ID_LENGTH = 256


def parse_platform(value: str) -> Platform:
    """Return the platform or raise ``InvalidInput`` (``ING_UNKNOWN_PLATFORM``)."""
    try:
        return Platform(value)
    except ValueError:
        raise InvalidInput(
            "Unknown platform", code=ec.ING_UNKNOWN_PLATFORM, platform=str(value)[:64]
        ) from None


def validate_external_id(platform: Platform | str, value: str) -> str:
    """Return ``value`` unchanged if valid, else raise ``InvalidInput``.

    - X: ASCII digits only, 1 to 19 characters, integer value ≤ ``X_MAX_ID``, no sign,
      no whitespace (leading zeros allowed but discouraged: keep as given).
    - other platforms: 1 to 256 characters, all printable (``str.isprintable``),
      no leading or trailing whitespace.
    Unknown platform → ``ING_UNKNOWN_PLATFORM``; bad value → ``ING_BAD_EXTERNAL_ID``
    (details: ``platform``, never the full value if longer than 64 characters).
    """
    plat = platform if isinstance(platform, Platform) else parse_platform(platform)
    if plat is Platform.X:
        ok = (
            isinstance(value, str)
            and value.isascii()
            and value.isdigit()
            and len(value) <= 19
            and int(value) <= X_MAX_ID
        )
    else:
        ok = (
            isinstance(value, str)
            and 1 <= len(value) <= MAX_EXTERNAL_ID_LENGTH
            and value.isprintable()
            and value == value.strip()
        )
    if not ok:
        raise InvalidInput(
            "Invalid platform identifier",
            code=ec.ING_BAD_EXTERNAL_ID,
            platform=plat.value,
            value=value[:64] if isinstance(value, str) else None,
        )
    return value
