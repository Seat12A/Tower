"""Tokens de service : aléatoires, préfixés, stockés uniquement sous forme d'empreinte."""

from __future__ import annotations

import hashlib
import secrets

TOKEN_PREFIX = "t12a_"  # noqa: S105 (préfixe public, pas un secret)


def generate_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> bytes:
    # SHA-256 suffit : le token a 256 bits d'entropie, pas besoin d'un hachage lent.
    return hashlib.sha256(token.encode()).digest()
