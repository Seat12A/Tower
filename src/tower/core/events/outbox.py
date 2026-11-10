"""Transactional outbox.

Writing
    ``publish()`` inserts the event into the current transaction and emits
    ``pg_notify``. PostgreSQL only delivers the notification at commit: on rollback,
    neither the event nor the notification exists.

Gap-free reading
    ``id`` values are assigned at insertion, but transactions finish in a different
    order: reading "id > last id seen" can skip an event that is not yet committed.
    So we read in ``(xid, id)`` order, keeping only transactions older than
    ``pg_snapshot_xmin(pg_current_snapshot())``, i.e. all transactions that have
    finished. Any event that becomes visible later will have an ``xid`` greater than
    that threshold, and therefore greater than the cursor.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import orjson
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain import error_codes as ec
from tower.core.domain.errors import InvalidInput
from tower.core.events.channels import EVENTS


@dataclass(frozen=True, slots=True, order=True)
class Cursor:
    xid: int = 0
    id: int = 0

    def encode(self) -> str:
        return f"{self.xid}-{self.id}"

    @classmethod
    def decode(cls, value: str | None) -> Cursor:
        if not value or value == "0":
            return cls()
        try:
            xid, id_ = value.split("-", 1)
            return cls(int(xid), int(id_))
        except ValueError as exc:
            raise InvalidInput("Invalid event cursor", code=ec.EVT_BAD_CURSOR, value=value) from exc


@dataclass(frozen=True, slots=True)
class EventRecord:
    id: int
    xid: int
    topic: str
    aggregate_type: str | None
    aggregate_id: str | None
    payload: dict[str, Any]
    actor: str | None
    created_at: datetime

    @property
    def cursor(self) -> Cursor:
        return Cursor(self.xid, self.id)

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.cursor.encode(),
            "topic": self.topic,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "payload": self.payload,
            "actor": self.actor,
            "created_at": self.created_at.isoformat(),
        }


_INSERT = text(
    """
    WITH ins AS (
        INSERT INTO events (topic, aggregate_type, aggregate_id, payload, actor)
        VALUES (:topic, :aggregate_type, :aggregate_id, CAST(:payload AS jsonb), :actor)
        RETURNING id
    )
    SELECT id, pg_notify(:channel, :topic) FROM ins
    """
)

_READ = text(
    """
    SELECT id, xid::text::bigint AS xid, topic, aggregate_type, aggregate_id,
           payload, actor, created_at
    FROM events
    WHERE (xid, id) > (CAST(:xid AS xid8), :id)
      AND xid < pg_snapshot_xmin(pg_current_snapshot())
      AND (CAST(:topics AS text[]) IS NULL OR topic = ANY(CAST(:topics AS text[])))
    ORDER BY xid, id
    LIMIT :limit
    """
)


async def publish(
    session: AsyncSession,
    topic: str,
    payload: dict[str, Any] | None = None,
    *,
    aggregate_type: str | None = None,
    aggregate_id: str | int | None = None,
    actor: str | None = None,
) -> int:
    """Append an event to the current transaction and return its id."""
    result = await session.execute(
        _INSERT,
        {
            "topic": topic,
            "aggregate_type": aggregate_type,
            "aggregate_id": None if aggregate_id is None else str(aggregate_id),
            "payload": orjson.dumps(payload or {}).decode(),
            "actor": actor,
            "channel": EVENTS,
        },
    )
    return int(result.scalar_one())


async def read_events(
    session: AsyncSession,
    after: Cursor,
    *,
    limit: int = 200,
    topics: list[str] | None = None,
) -> list[EventRecord]:
    """Read committed events after ``after``, in order."""
    rows = await session.execute(
        _READ, {"xid": after.xid, "id": after.id, "topics": topics, "limit": limit}
    )
    return [EventRecord(**row._mapping) for row in rows]
