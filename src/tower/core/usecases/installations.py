"""Daily secrets of the installation pseudonyms (shared by ingestion and maintenance)."""

from __future__ import annotations

import secrets
from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain.privacy import SECRET_BYTES

_ENSURE = text(
    "INSERT INTO installation_secrets (day, secret) VALUES (:day, :secret) "
    "ON CONFLICT (day) DO NOTHING"
)
_GET = text("SELECT secret FROM installation_secrets WHERE day = :day")


async def daily_secret(session: AsyncSession, day: date) -> bytes:
    """Secret of ``day`` (UTC), created if missing. Concurrent callers get the same one."""
    await session.execute(_ENSURE, {"day": day, "secret": secrets.token_bytes(SECRET_BYTES)})
    secret: bytes = (await session.execute(_GET, {"day": day})).scalar_one()
    return secret
