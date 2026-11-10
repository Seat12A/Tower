"""Service tokens: random, prefixed, stored only as a hash."""

from __future__ import annotations

import hashlib
import secrets

TOKEN_PREFIX = "t12a_"  # noqa: S105 (public prefix, not a secret)


def generate_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> bytes:
    # SHA-256 is enough: the token has 256 bits of entropy, so no slow hash is needed.
    return hashlib.sha256(token.encode()).digest()
