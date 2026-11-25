"""Short identifiers: a member may type the first characters of a public id (UUID)."""

from __future__ import annotations

import re
import uuid

from tower.core.domain import error_codes as ec
from tower.core.domain.errors import InvalidInput

__all__ = ["MIN_PREFIX", "SHORT_ID_LEN", "parse_ref", "prefix_bounds"]

MIN_PREFIX = 6
"""Shortest accepted prefix (the bot shows 8 characters)."""

SHORT_ID_LEN = 8
"""Length of a case short id (``cases.short_id``); longer only on a prefix collision."""

_HEX = re.compile(r"^[0-9a-f]+$")


def prefix_bounds(ref: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Smallest and largest UUID starting with ``ref`` (dashes ignored, any case), so
    the lookup is a range scan on the unique index. ``InvalidInput`` otherwise."""
    digits = ref.strip().lower().replace("-", "")
    if not _HEX.fullmatch(digits) or not MIN_PREFIX <= len(digits) <= 32:
        raise InvalidInput(
            f"Expected an identifier or its first {MIN_PREFIX} characters or more",
            code=ec.GEN_INVALID_INPUT,
        )
    return uuid.UUID(digits.ljust(32, "0")), uuid.UUID(digits.ljust(32, "f"))


def parse_ref(ref: str) -> uuid.UUID | str:
    """A full public id (UUID) or a short id (``SHORT_ID_LEN`` to 31 hex characters,
    dashes ignored, any case). ``InvalidInput`` otherwise."""
    digits = ref.strip().lower().replace("-", "")
    if not _HEX.fullmatch(digits) or not SHORT_ID_LEN <= len(digits) <= 32:
        raise InvalidInput(
            f"Expected a short id ({SHORT_ID_LEN} characters or more) or a full id",
            code=ec.GEN_INVALID_INPUT,
        )
    return uuid.UUID(digits) if len(digits) == 32 else digits
