"""Installation pseudonyms: count independent observers without any profiling.

The extension sends a random token. Tower never stores it: it stores
``HMAC-SHA256(secret_of_the_day, token)`` truncated to 16 bytes. The secret of each
UTC day is destroyed after that day, so pseudonyms of two different days cannot be
linked. Standard library only.
"""

from __future__ import annotations

PSEUDONYM_BYTES = 16
SECRET_BYTES = 32
TOKEN_MIN_LENGTH = 22
TOKEN_MAX_LENGTH = 64


def is_valid_installation_token(token: str) -> bool:
    """base64url alphabet only (``A-Z a-z 0-9 - _``, no padding), 22 to 64 characters."""
    raise NotImplementedError


def validate_installation_token(token: str | None) -> str:
    """Return the token, or raise ``Unauthenticated`` with ``ING_INVALID_INSTALLATION_TOKEN``
    (never put the token in the error details)."""
    raise NotImplementedError


def installation_pseudonym(secret: bytes, token: str) -> bytes:
    """``hmac.new(secret, token.encode("ascii"), sha256).digest()[:16]``.

    Raises ``ValueError`` if ``secret`` is shorter than 16 bytes.
    """
    raise NotImplementedError


def new_daily_secret() -> bytes:
    """``secrets.token_bytes(SECRET_BYTES)``."""
    raise NotImplementedError
