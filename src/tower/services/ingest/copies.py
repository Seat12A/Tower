"""Copy linking: ``content_items.copy_of_id`` points to the oldest copy (or its root)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain.text import simhash_bands
from tower.core.services.base import JobContext

NEAR_WINDOW = timedelta(days=30)
MAX_DISTANCE = 3

_CURRENT = text(
    "SELECT first_seen_at, content_hash, simhash, copy_of_id FROM content_items WHERE id = :id"
)

_EXACT = text(
    """
    SELECT id, copy_of_id FROM content_items
    WHERE content_hash = :hash AND id <> :id
      AND (first_seen_at, id) < (:seen, :id)
    ORDER BY first_seen_at, id
    LIMIT 1
    """
)

_BAND_QUERY = """(SELECT a.id, a.copy_of_id, a.first_seen_at, a.simhash FROM content_items a
        WHERE a.simhash_b@ = :b@ AND a.first_seen_at >= :since
          AND (a.first_seen_at, a.id) < (:seen, :id)
        ORDER BY a.first_seen_at LIMIT 500)"""
_NEAR = text(
    "SELECT id, copy_of_id FROM (\n"  # noqa: S608
    + "\nUNION\n".join(_BAND_QUERY.replace("@", str(i)) for i in range(4))
    + """
) c
    WHERE bit_count((c.simhash # :simhash)::bit(64)) <= :max_distance
    ORDER BY c.first_seen_at, c.id
    LIMIT 1
    """
)

_LINK = text("UPDATE content_items SET copy_of_id = :orig WHERE id = :id AND copy_of_id IS NULL")


async def link_copy(session: AsyncSession, content_id: int, *, now: datetime) -> int | None:
    """Link ``content_id`` to the oldest earlier copy. Returns the original id, or None."""
    linked = await _link(session, content_id, now=now)
    return None if linked is None else linked[0]


async def _link(session: AsyncSession, content_id: int, *, now: datetime) -> tuple[int, str] | None:
    row = (await session.execute(_CURRENT, {"id": content_id})).first()
    if row is None or row.copy_of_id is not None:
        return None
    seen: datetime = row.first_seen_at
    kind = "exact"
    candidate = (
        await session.execute(_EXACT, {"hash": row.content_hash, "id": content_id, "seen": seen})
    ).first()
    if candidate is None:
        kind = "near"
        params: dict[str, Any] = {
            f"b{i}": band for i, band in enumerate(simhash_bands(row.simhash))
        }
        params.update(
            id=content_id,
            seen=seen,
            since=now - NEAR_WINDOW,
            simhash=row.simhash,
            max_distance=MAX_DISTANCE,
        )
        candidate = (await session.execute(_NEAR, params)).first()
    if candidate is None:
        return None
    original: int = candidate.copy_of_id if candidate.copy_of_id is not None else candidate.id
    result = await session.execute(_LINK, {"orig": original, "id": content_id})
    if not result.rowcount:  # type: ignore[attr-defined]
        return None
    return original, kind


async def link_copies(ctx: JobContext) -> None:
    content_id = int(ctx.job.payload["content_id"])
    async with ctx.db.transaction() as session:
        linked = await _link(session, content_id, now=datetime.now(UTC))
    if linked is not None:
        original_id, kind = linked
        ctx.log.info(
            "ingest.copy_linked", content_id=content_id, original_id=original_id, kind=kind
        )
