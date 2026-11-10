from __future__ import annotations

import asyncio

from tower.core.db.database import Database
from tower.core.events.outbox import Cursor, publish, read_events
from tower.hangar.main import serve

from .conftest import make_settings


async def test_ping_roundtrip_through_hangar(db: Database) -> None:
    """Événement → consommateur → job → handler → événement, via le vrai processus."""
    settings = make_settings(services="ping", process="hangar")
    stop = asyncio.Event()
    task = asyncio.create_task(serve(settings, stop))
    try:
        async with db.transaction() as s:
            await publish(s, "ping.requested", {"hello": "tower"})
        async with asyncio.timeout(10):
            while True:
                async with db.session() as s:
                    topics = {e.topic: e for e in await read_events(s, Cursor())}
                if "ping.done" in topics:
                    break
                await asyncio.sleep(0.1)
        assert topics["ping.done"].payload["request"]["hello"] == "tower"
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)
