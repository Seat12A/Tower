"""Une connexion ``LISTEN`` par processus, redistribuée aux tâches qui attendent.

Les notifications servent uniquement à réveiller : chaque consommateur relit la base
à son réveil, et un délai maximal (polling de secours) couvre les notifications
perdues, par exemple pendant une reconnexion.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from typing import Any

import asyncpg

from tower.core.domain import error_codes as ec
from tower.core.errors import capture
from tower.core.logging import get_logger

log = get_logger(__name__)


class Waker:
    """Réveil pour un abonné. ``wait()`` rend la main sur notification ou au délai."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def set(self) -> None:
        self._event.set()

    async def wait(self, timeout: float | None) -> bool:  # noqa: ASYNC109
        """Renvoie True si réveillé par une notification, False si délai écoulé."""
        try:
            async with asyncio.timeout(timeout):
                await self._event.wait()
        except TimeoutError:
            return False
        finally:
            self._event.clear()
        return True


class PgListener:
    def __init__(self, dsn: str, channels: list[str], *, ssl: bool | None = None) -> None:
        self._dsn = dsn
        self._channels = channels
        self._ssl = ssl
        self._wakers: dict[str, set[Waker]] = defaultdict(set)
        self._task: asyncio.Task[None] | None = None
        self._conn: Any = None
        self.connected = asyncio.Event()

    def subscribe(self, channel: str) -> Waker:
        if channel not in self._channels:
            raise ValueError(f"Canal non écouté : {channel}")
        waker = Waker()
        self._wakers[channel].add(waker)
        return waker

    def unsubscribe(self, channel: str, waker: Waker) -> None:
        self._wakers[channel].discard(waker)

    def _on_notify(self, _conn: Any, _pid: int, channel: str, _payload: str) -> None:
        for waker in self._wakers.get(channel, ()):
            waker.set()

    def _wake_all(self) -> None:
        for wakers in self._wakers.values():
            for waker in wakers:
                waker.set()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="pg-listener")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._close()

    async def _close(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close(timeout=2)
            self._conn = None
        self.connected.clear()

    async def _run(self) -> None:
        backoff = 0.5
        while True:
            try:
                self._conn = await asyncpg.connect(
                    self._dsn,
                    ssl=self._ssl,
                    server_settings={"application_name": "tower:listener"},
                )
                for channel in self._channels:
                    await self._conn.add_listener(channel, self._on_notify)
                self.connected.set()
                log.info("listener.connected", channels=self._channels)
                backoff = 0.5
                self._wake_all()  # rattrape ce qui a pu être manqué
                while not self._conn.is_closed():
                    await asyncio.sleep(5)
                    await self._conn.execute("SELECT 1")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                capture(
                    ec.DB_LISTENER_DOWN, exc, channels=",".join(self._channels), retry_in_s=backoff
                )
            await self._close()
            self._wake_all()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
