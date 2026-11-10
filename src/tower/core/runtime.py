"""Common lifecycle for long-running processes (hangar, radar, intercom).

Railway sends SIGTERM on redeploy, then SIGKILL after ``drainingSeconds``.
We stop taking new work as soon as SIGTERM arrives and cleanly finish what is running.
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
    """Run ``main(stop)`` under uvloop (if available), with signal handling."""

    async def _entry() -> None:
        stop = asyncio.Event()
        install_signal_handlers(stop)
        await main(stop)

    try:
        import uvloop

        uvloop.run(_entry())
    except ImportError:
        asyncio.run(_entry())
