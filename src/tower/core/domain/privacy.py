"""Installation pseudonyms: count independent observers without any profiling.

The extension sends a random token. Tower never stores it: it stores
``HMAC-SHA256(secret_of_the_day, token)`` truncated to 16 bytes. The secret of each
UTC day is destroyed after that day, so pseudonyms of two different days cannot be
linked. Standard library only.
"""

from __future__ import annotations

import hmac
import re
import secrets
from hashlib import sha256

from tower.core.domain import error_codes as ec
from tower.core.domain.errors import Unauthenticated

_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{22,64}")
_MIN_SECRET_BYTES = 16

PSEUDONYM_BYTES = 16
SECRET_BYTES = 32
TOKEN_MIN_LENGTH = 22
TOKEN_MAX_LENGTH = 64


def is_valid_installation_token(token: str) -> bool:
    """base64url alphabet only (``A-Z a-z 0-9 - _``, no padding), 22 to 64 characters."""
    return isinstance(token, str) and _TOKEN_RE.fullmatch(token) is not None


def validate_installation_token(token: str | None) -> str:
    """Return the token, or raise ``Unauthenticated`` with ``ING_INVALID_INSTALLATION_TOKEN``
    (never put the token in the error details)."""
    if token is None or not is_valid_installation_token(token):
        raise Unauthenticated("Invalid installation token", code=ec.ING_INVALID_INSTALLATION_TOKEN)
    return token


def installation_pseudonym(secret: bytes, token: str) -> bytes:
    """``hmac.new(secret, token.encode("ascii"), sha256).digest()[:16]``.

    Raises ``ValueError`` if ``secret`` is shorter than 16 bytes.
    """
    if len(secret) < _MIN_SECRET_BYTES:
        raise ValueError(f"Secret must be at least {_MIN_SECRET_BYTES} bytes")
    return hmac.new(secret, token.encode("ascii"), sha256).digest()[:PSEUDONYM_BYTES]


def new_daily_secret() -> bytes:
    """``secrets.token_bytes(SECRET_BYTES)``."""
    return secrets.token_bytes(SECRET_BYTES)
