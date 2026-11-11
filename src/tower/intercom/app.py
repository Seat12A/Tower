"""discord.py client of intercom (the only module importing ``discord``)."""

from __future__ import annotations

import asyncio

from tower.core.config import Settings


async def serve(settings: Settings, stop: asyncio.Event, *, dry_run: bool = False) -> None:
    """Run the bot until ``stop``. ``dry_run``: build everything (gate client, notifier,
    command tree) without connecting to Discord, then wait for ``stop``."""
    raise NotImplementedError
