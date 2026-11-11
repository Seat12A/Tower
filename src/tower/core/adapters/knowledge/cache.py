"""``source_cache`` over PostgreSQL: a fresh response is never requested (or paid) twice."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import orjson
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from tower.core.models.factcheck import SourceCacheEntry

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from tower.core.db.database import Database

__all__ = ["DbSourceCache", "cache_key", "purge_expired"]


def cache_key(adapter: str, request: Mapping[str, object]) -> bytes:
    """SHA-256 of the adapter name and the canonical JSON of the request."""
    canonical = orjson.dumps(
        {"adapter": adapter, "request": dict(request)},
        option=orjson.OPT_SORT_KEYS,
        default=str,
    )
    return hashlib.sha256(canonical).digest()


class DbSourceCache:
    """``SourceCache`` implementation: one short transaction per call."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, adapter: str, request: Mapping[str, object]) -> dict[str, Any] | None:
        key = cache_key(adapter, request)
        async with self._db.session() as session:
            row = (
                await session.execute(
                    select(SourceCacheEntry.response).where(
                        SourceCacheEntry.key == key,
                        SourceCacheEntry.expires_at > datetime.now(UTC),
                    )
                )
            ).scalar_one_or_none()
        return dict(row) if row is not None else None

    async def put(
        self,
        adapter: str,
        request: Mapping[str, object],
        response: Mapping[str, Any],
        *,
        ttl_s: int,
    ) -> None:
        now = datetime.now(UTC)
        values = {
            "key": cache_key(adapter, request),
            "adapter": adapter,
            "response": dict(response),
            "fetched_at": now,
            "expires_at": now + timedelta(seconds=ttl_s),
        }
        stmt = pg_insert(SourceCacheEntry).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[SourceCacheEntry.key],
            set_={k: stmt.excluded[k] for k in ("adapter", "response", "fetched_at", "expires_at")},
        )
        async with self._db.transaction() as session:
            await session.execute(stmt)


async def purge_expired(session: AsyncSession, now: datetime, *, limit: int = 1000) -> int:
    """Delete up to ``limit`` expired rows; returns how many were deleted."""
    expired = select(SourceCacheEntry.key).where(SourceCacheEntry.expires_at <= now).limit(limit)
    result = await session.execute(
        delete(SourceCacheEntry).where(SourceCacheEntry.key.in_(expired))
    )
    return int(getattr(result, "rowcount", 0) or 0)
