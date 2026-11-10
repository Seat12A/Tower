"""Named outbox consumer, with a persistent cursor.

Each batch is processed in **a single transaction**: lock the cursor, read, call the
handler, advance the cursor. The handler's database effects and the cursor advance are
therefore atomic (exactly once for the database).

The ``FOR UPDATE SKIP LOCKED`` lock on the cursor row ensures that only one replica
processes a given consumer at a time; the others wait their turn without error.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.db.database import Database
from tower.core.domain import error_codes as ec
from tower.core.errors import capture
from tower.core.events.channels import EVENTS
from tower.core.events.listener import PgListener
from tower.core.events.outbox import Cursor, EventRecord, read_events
from tower.core.logging import get_logger

log = get_logger(__name__)

EventHandler = Callable[[AsyncSession, Sequence[EventRecord]], Awaitable[None]]

_ENSURE = text("INSERT INTO event_cursors (consumer) VALUES (:c) ON CONFLICT (consumer) DO NOTHING")
_LOCK = text(
    "SELECT last_xid, last_id FROM event_cursors WHERE consumer = :c FOR UPDATE SKIP LOCKED"
)
_SAVE = text(
    "UPDATE event_cursors SET last_xid = :x, last_id = :i, updated_at = now() WHERE consumer = :c"
)


async def consume_batch(
    db: Database,
    consumer: str,
    handler: EventHandler,
    *,
    topics: list[str] | None = None,
    limit: int = 100,
) -> int:
    """Process one batch. Return the number of events processed (0 if none or locked)."""
    async with db.transaction() as session:
        await session.execute(_ENSURE, {"c": consumer})
    async with db.transaction() as session:
        row = (await session.execute(_LOCK, {"c": consumer})).first()
        if row is None:
            return 0  # another replica is processing this consumer
        events = await read_events(
            session, Cursor(row.last_xid, row.last_id), limit=limit, topics=topics
        )
        if not events:
            return 0
        await handler(session, events)
        last = events[-1].cursor
        await session.execute(_SAVE, {"x": last.xid, "i": last.id, "c": consumer})
        return len(events)


async def run_consumer(
    db: Database,
    listener: PgListener,
    consumer: str,
    handler: EventHandler,
    *,
    topics: list[str] | None = None,
    poll_interval: float = 5.0,
    stop: asyncio.Event,
) -> None:
    waker = listener.subscribe(EVENTS)
    try:
        while not stop.is_set():
            try:
                processed = await consume_batch(db, consumer, handler, topics=topics)
            except Exception as exc:
                capture(ec.EVT_CONSUMER_FAILED, exc, consumer=consumer)
                processed = 0
                await asyncio.sleep(poll_interval)
            if processed == 0:
                await waker.wait(poll_interval)
    finally:
        listener.unsubscribe(EVENTS, waker)
