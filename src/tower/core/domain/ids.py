"""Short identifiers: a member may type the first characters of a public id (UUID)."""

from __future__ import annotations

import re
import uuid

from tower.core.domain import error_codes as ec
from tower.core.domain.errors import InvalidInput

__all__ = ["MIN_PREFIX", "prefix_bounds"]

MIN_PREFIX = 6
"""Shortest accepted prefix (the bot shows 8 characters)."""

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
