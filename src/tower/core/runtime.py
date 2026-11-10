"""Cycle de vie commun des processus longs (hangar, radar, intercom).

Railway envoie SIGTERM au redéploiement, puis SIGKILL après ``drainingSeconds``.
On arrête de prendre du travail dès SIGTERM et on termine proprement ce qui tourne.
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Awaitable, Callable

from tower.core.logging import get_logger

log = get_logger(__name__)


def install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def _handle(sig: signal.Signals) -> None:
        if stop.is_set():
            return
        log.info("process.stopping", signal=sig.name)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle, sig)


def run(main: Callable[[asyncio.Event], Awaitable[None]]) -> None:
    """Lance ``main(stop)`` sous uvloop (si disponible) avec gestion des signaux."""

    async def _entry() -> None:
        stop = asyncio.Event()
        install_signal_handlers(stop)
        await main(stop)

    try:
        import uvloop

        uvloop.run(_entry())
    except ImportError:
        asyncio.run(_entry())
