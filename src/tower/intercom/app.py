"""discord.py client of intercom (the only module importing ``discord``).

No ``from __future__ import annotations`` here on purpose: discord.py inspects the
annotations of command callbacks, which are defined inside functions.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

from tower.core.config import Settings
from tower.core.domain import error_codes as ec
from tower.core.domain.platforms import Platform, PublicRole
from tower.core.errors import capture
from tower.core.logging import get_logger
from tower.intercom.gate_client import GateClient
from tower.intercom.handlers import Reply
from tower.intercom.i18n import locale_of, t
from tower.intercom.notifier import Notifier

log = get_logger(__name__)

Sender = Callable[[str, str], Awaitable[None]]
HandlerCall = Callable[[str, int], Awaitable[Reply]]


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
    locale = locale_of(settings.discord_locale)
    channel_ids: dict[str, int | None] = {
        "incidents": settings.discord_channel_incidents,
        "budget": settings.discord_channel_budget,
        "dossiers": settings.discord_channel_dossiers,
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
        runner = asyncio.create_task(client.start(discord_token))
        waiter = asyncio.create_task(stop.wait())
        tasks += [runner, waiter]
        await asyncio.wait({runner, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if runner.done() and not stop.is_set():
            runner.result()  # login or connection failure: raise so the process restarts
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
    """Discord client with its command tree and notifier, not connected."""
    import discord
    from discord import app_commands

    intents = discord.Intents.none()
    intents.guilds = True  # channel cache, nothing else (no members, no message content)

    class Intercom(discord.Client):
        tree: app_commands.CommandTree
        notifier: Notifier
        notifier_task: asyncio.Task[None] | None = None

        async def setup_hook(self) -> None:
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
    _register_commands(client.tree, gate)

    async def send_discord(channel_key: str, text: str) -> None:
        channel_id = channel_ids.get(channel_key)
        if not channel_id:
            return
        channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            raise TypeError(f"channel {channel_key} is not messageable")
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())

    async def send_dry_run(channel_key: str, text: str) -> None:
        log.info("intercom.notification", channel=channel_key, text_length=len(text))

    client.notifier = Notifier(
        gate,
        send_dry_run if dry_run else send_discord,
        cursor_name=settings.intercom_cursor_name,
        locale=default_locale,
    )
    return client


def _choices(values: type[Platform] | type[PublicRole]) -> list[Any]:
    from discord import app_commands

    return [app_commands.Choice(name=v.value.replace("_", " "), value=v.value) for v in values]


def _register_commands(tree: Any, gate: GateClient) -> None:
    import discord
    from discord import app_commands

    from tower.intercom.handlers import dossiers, incidents, usage, watchlist

    async def run(interaction: Any, call: HandlerCall) -> None:
        locale = locale_of(str(interaction.locale))
        # Deferring first: gate may take longer than Discord's 3 s acknowledgement window.
        await interaction.response.defer(ephemeral=True)
        try:
            reply = await call(locale, interaction.user.id)
        except Exception as exc:
            capture(ec.INT_UNEXPECTED, exc, command=getattr(interaction.command, "name", None))
            reply = Reply(
                t(locale, "error.generic", message="Internal error", code=ec.INT_UNEXPECTED.code),
                ephemeral=True,
            )
        await interaction.followup.send(reply.text, ephemeral=reply.ephemeral)

    platform_choices = _choices(Platform)
    category_choices = _choices(PublicRole)

    # --- /watchlist ----------------------------------------------------------------
    wl = app_commands.Group(name="watchlist", description="Watched accounts")

    @wl.command(name="list", description="List watched accounts")
    @app_commands.describe(platform="Only this platform")
    @app_commands.choices(platform=platform_choices)
    async def wl_list(
        interaction: discord.Interaction, platform: app_commands.Choice[str] | None = None
    ) -> None:
        value = platform.value if platform else None
        await run(interaction, lambda loc, uid: watchlist.list_accounts(gate, uid, loc, value))

    @wl.command(name="add", description="Add an account by handle (resolved by the radar)")
    @app_commands.describe(
        platform="Platform", handle="Account handle", category="Public role", priority="-100..100"
    )
    @app_commands.choices(platform=platform_choices, category=category_choices)
    async def wl_add(
        interaction: discord.Interaction,
        platform: app_commands.Choice[str],
        handle: str,
        category: app_commands.Choice[str],
        priority: app_commands.Range[int, -100, 100] = 0,
        note: str | None = None,
    ) -> None:
        await run(
            interaction,
            lambda loc, uid: watchlist.add(
                gate, uid, loc, platform.value, handle, category.value, priority, note
            ),
        )

    @wl.command(name="edit", description="Edit a watched account")
    @app_commands.describe(public_id="Public id (at least 6 characters)")
    @app_commands.choices(category=category_choices)
    async def wl_edit(
        interaction: discord.Interaction,
        public_id: str,
        category: app_commands.Choice[str] | None = None,
        priority: app_commands.Range[int, -100, 100] | None = None,
        note: str | None = None,
        handle: str | None = None,
    ) -> None:
        cat = category.value if category else None
        await run(
            interaction,
            lambda loc, uid: watchlist.edit(gate, uid, loc, public_id, cat, priority, note, handle),
        )

    @wl.command(name="deactivate", description="Stop watching an account")
    @app_commands.describe(public_id="Public id (at least 6 characters)")
    async def wl_deactivate(interaction: discord.Interaction, public_id: str) -> None:
        await run(interaction, lambda loc, uid: watchlist.deactivate(gate, uid, loc, public_id))

    # --- /dossier ------------------------------------------------------------------
    dos = app_commands.Group(name="dossier", description="Fact-check dossiers")

    @dos.command(name="show", description="Show a dossier")
    async def d_show(interaction: discord.Interaction, public_id: str) -> None:
        await run(interaction, lambda loc, uid: dossiers.show(gate, uid, loc, public_id))

    @dos.command(name="take", description="Take a dossier into review")
    async def d_take(interaction: discord.Interaction, public_id: str) -> None:
        await run(interaction, lambda loc, uid: dossiers.take(gate, uid, loc, public_id))

    @dos.command(name="validate", description="Validate a dossier")
    async def d_validate(
        interaction: discord.Interaction, public_id: str, note: str | None = None
    ) -> None:
        await run(interaction, lambda loc, uid: dossiers.validate(gate, uid, loc, public_id, note))

    @dos.command(name="reject", description="Reject a dossier")
    async def d_reject(
        interaction: discord.Interaction, public_id: str, note: str | None = None
    ) -> None:
        await run(interaction, lambda loc, uid: dossiers.reject(gate, uid, loc, public_id, note))

    @dos.command(name="publish", description="Record the X publication link (manual post)")
    @app_commands.describe(url="https://x.com/<account>/status/<id>")
    async def d_publish(interaction: discord.Interaction, public_id: str, url: str) -> None:
        await run(interaction, lambda loc, uid: dossiers.publish(gate, uid, loc, public_id, url))

    # --- /incidents ----------------------------------------------------------------
    inc = app_commands.Group(name="incidents", description="Stored errors")

    @inc.command(name="list", description="List open incidents")
    async def i_list(interaction: discord.Interaction) -> None:
        await run(interaction, lambda loc, uid: incidents.list_incidents(gate, uid, loc))

    @inc.command(name="resolve", description="Resolve an incident")
    @app_commands.describe(fingerprint="Fingerprint (at least 6 characters)")
    async def i_resolve(
        interaction: discord.Interaction, fingerprint: str, note: str | None = None
    ) -> None:
        await run(
            interaction, lambda loc, uid: incidents.resolve(gate, uid, loc, fingerprint, note)
        )

    # --- /usage --------------------------------------------------------------------
    @app_commands.command(name="usage", description="LLM free allocation and spend")
    async def u_show(interaction: discord.Interaction) -> None:
        await run(interaction, lambda loc, uid: usage.show_usage(gate, uid, loc))

    for group in (wl, dos, inc):
        tree.add_command(group)
    tree.add_command(u_show)
