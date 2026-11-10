from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.db.database import Database
from tower.core.domain.errors import InvalidInput
from tower.core.events.consumer import consume_batch
from tower.core.events.outbox import Cursor, EventRecord, publish, read_events
from tower.core.models.events import Event


async def _all(db: Database, after: Cursor | None = None) -> list[EventRecord]:
    async with db.session() as s:
        return await read_events(s, after or Cursor())


async def test_publish_is_transactional(db: Database) -> None:
    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        async with db.transaction() as s:
            await publish(s, "test.rolled_back", {"a": 1})
            raise Boom
    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(Event)) == 0


async def test_cursor_roundtrip_and_validation() -> None:
    assert Cursor.decode(Cursor(12, 34).encode()) == Cursor(12, 34)
    assert Cursor.decode(None) == Cursor()
    with pytest.raises(InvalidInput) as info:
        Cursor.decode("pas-un-curseur")
    assert info.value.code == "TWR-EVT-001"


async def test_open_transaction_hides_later_commits(db: Database) -> None:
    """Un événement validé après une transaction encore ouverte n'est pas lu trop tôt."""
    async with db.sessionmaker() as slow:
        await slow.begin()
        await publish(slow, "test.slow")  # id 1, transaction ouverte
        async with db.transaction() as fast:
            await publish(fast, "test.fast")  # id 2, validé
        assert await _all(db) == []  # rien n'est lu tant que "slow" est ouverte
        await slow.commit()
    events = await _all(db)
    assert [e.topic for e in events] == ["test.slow", "test.fast"]
    assert await _all(db, events[-1].cursor) == []


async def test_topic_filter(db: Database) -> None:
    async with db.transaction() as s:
        await publish(s, "a.one")
        await publish(s, "b.two")
    async with db.session() as s:
        events = await read_events(s, Cursor(), topics=["b.two"])
    assert [e.topic for e in events] == ["b.two"]


async def test_consumer_advances_cursor_atomically(db: Database) -> None:
    async with db.transaction() as s:
        for i in range(3):
            await publish(s, "test.item", {"i": i})

    seen: list[int] = []

    async def handler(_: AsyncSession, events: Sequence[EventRecord]) -> None:
        seen.extend(e.payload["i"] for e in events)

    assert await consume_batch(db, "c1", handler, limit=2) == 2
    assert await consume_batch(db, "c1", handler, limit=2) == 1
    assert await consume_batch(db, "c1", handler) == 0
    assert seen == [0, 1, 2]

    async def failing(_: AsyncSession, __: Sequence[EventRecord]) -> None:
        raise RuntimeError("boom")

    async with db.transaction() as s:
        await publish(s, "test.item", {"i": 3})
    with pytest.raises(RuntimeError):
        await consume_batch(db, "c1", failing)
    assert await consume_batch(db, "c1", handler) == 1  # le lot est rejoué
    assert seen == [0, 1, 2, 3]


async def test_same_consumer_never_runs_twice_concurrently(db: Database) -> None:
    async with db.transaction() as s:
        await publish(s, "test.item", {"i": 0})
    gate = asyncio.Event()
    calls = 0

    async def slow(_: AsyncSession, events: Sequence[EventRecord]) -> None:
        nonlocal calls
        calls += 1
        await gate.wait()

    first = asyncio.create_task(consume_batch(db, "c2", slow))
    await asyncio.sleep(0.2)
    assert await consume_batch(db, "c2", slow) == 0  # verrouillé : sauté sans erreur
    gate.set()
    assert await first == 1
    assert calls == 1
