"""discord.py client of intercom.

``discord`` is imported only by the bot process: here (lazily), in ``ui.render`` and
``ui.base``, in ``commands/`` and in ``delivery`` (all loaded through ``serve``).
"""

import asyncio
import contextlib
from typing import Any

from tower.core.config import Settings
from tower.core.domain import error_codes as ec
from tower.core.errors import capture
from tower.core.logging import get_logger
from tower.intercom.gate_client import GateClient
from tower.intercom.handlers.forum import PUBLIC_LOCALE
from tower.intercom.notifier import Notifier

log = get_logger(__name__)


async def serve(settings: Settings, stop: asyncio.Event, *, dry_run: bool = False) -> None:
    """Run the bot until ``stop``. ``dry_run``: build everything (gate client, notifier,
    command tree) without connecting to Discord, then wait for ``stop``."""
    gate_token = settings.gate_token.get_secret_value() if settings.gate_token else ""
    discord_token = settings.discord_token.get_secret_value() if settings.discord_token else ""
    missing = [
        name
        for name, value, needed in (
            ("TOWER_GATE_TOKEN", gate_token, True),
            ("TOWER_DISCORD_TOKEN", discord_token, not dry_run),
        )
        if needed and not value
    ]
    if missing:
        capture(ec.BOT_BAD_CONFIG, None, missing=missing)
        raise SystemExit(2)

    gate = GateClient(settings.gate_url, gate_token)
    locale = PUBLIC_LOCALE  # public messages: readers' language unknown
    forum_id = settings.discord_forum_dossiers
    channel_ids: dict[str, int | None] = {
        "incidents": settings.discord_channel_incidents,
        "budget": settings.discord_channel_budget,
        "dossiers": None if forum_id else settings.discord_channel_dossiers,
        "narratives": settings.discord_channel_narratives
        or (None if forum_id else settings.discord_channel_dossiers),
        "admin": settings.discord_channel_admin,
    }

    client = _build_client(settings, gate, stop, channel_ids, locale, dry_run)
    tasks: list[asyncio.Task[Any]] = []
    try:
        if dry_run:
            tasks.append(asyncio.create_task(client.notifier.run(stop)))
            log.info("intercom.dry_run", commands=len(client.tree.get_commands()))
            await stop.wait()
            return
        import discord

        runner = asyncio.create_task(client.start(discord_token))
        waiter = asyncio.create_task(stop.wait())
        tasks += [runner, waiter]
        await asyncio.wait({runner, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if runner.done() and not stop.is_set():
            exc = runner.exception()
            if isinstance(exc, discord.LoginFailure):
                # Rejected token: a clean, stored-free log line instead of a traceback.
                capture(ec.BOT_BAD_CONFIG, exc, reason="discord_login_failed")
                raise SystemExit(2)
            runner.result()  # other connection failure: raise so the process restarts
    finally:
        stop.set()
        with contextlib.suppress(Exception):
            await client.close()
        notifier_task = getattr(client, "notifier_task", None)
        if notifier_task is not None:
            tasks.append(notifier_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await gate.aclose()
        log.info("intercom.stopped")


def _build_client(
    settings: Settings,
    gate: GateClient,
    stop: asyncio.Event,
    channel_ids: dict[str, int | None],
    default_locale: str,
    dry_run: bool,
) -> Any:
    """Discord client with its command tree, persistent components and notifier, not
    connected."""
    import discord
    from discord import app_commands

    from tower.intercom import commands, delivery
    from tower.intercom.forum import DossierForum, ForumSink

    intents = discord.Intents.none()
    intents.guilds = True  # channel and role cache, nothing else (no members, no content)
    ctx = commands.Context(gate=gate, default_locale=default_locale)

    class Intercom(discord.Client):
        tree: app_commands.CommandTree
        notifier: Notifier
        notifier_task: asyncio.Task[None] | None = None

        async def setup_hook(self) -> None:
            # Buttons and selects of messages sent before a restart keep working.
            self.add_dynamic_items(*commands.dynamic_items(ctx))
            guild = discord.Object(settings.discord_guild_id) if settings.discord_guild_id else None
            if guild is not None:
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            else:
                await self.tree.sync()
            log.info("intercom.commands_synced", guild_id=settings.discord_guild_id)
            self.notifier_task = asyncio.create_task(self.notifier.run(stop))

    client = Intercom(
        intents=intents,
        member_cache_flags=discord.MemberCacheFlags.none(),
        chunk_guilds_at_startup=False,
        max_messages=None,
    )
    client.tree = app_commands.CommandTree(client)
    commands.register_all(client.tree, ctx)
    send, send_draft = delivery.build(client, settings, channel_ids, dry_run=dry_run)
    forum: DossierForum | None = None
    if settings.discord_forum_dossiers:
        sink: ForumSink = (
            delivery.DryRunForum()
            if dry_run
            else delivery.ForumDelivery(client, settings.discord_forum_dossiers)
        )
        forum = DossierForum(gate, sink)
    client.notifier = Notifier(
        gate,
        send,
        cursor_name=settings.intercom_cursor_name,
        locale=default_locale,
        send_draft=send_draft,
        alert_role_id=settings.discord_alert_role_id,
        forum=forum,
    )
    return client
