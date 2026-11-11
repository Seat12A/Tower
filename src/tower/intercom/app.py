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
from tower.intercom.handlers import factcheck as fc
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
    """Discord client with its command tree and notifier, not connected."""
    import discord
    from discord import app_commands

    intents = discord.Intents.none()
    intents.guilds = True  # channel cache, nothing else (no members, no message content)

    class Intercom(discord.Client):
        tree: app_commands.CommandTree
        notifier: Notifier
        notifier_task: asyncio.Task[None] | None = None
        draft_item: Any = None

        async def setup_hook(self) -> None:
            if self.draft_item is not None:
                # Buttons of draft messages posted before a restart keep working.
                self.add_dynamic_items(self.draft_item)
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
    draft_button = _register_draft_ui(gate)
    client.draft_item = draft_button

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

    async def send_draft_discord(channel_key: str, parts: list[str], draft_id: str) -> bool:
        """Post the draft parts; the three buttons go on the last message."""
        channel_id = channel_ids.get(channel_key)
        if not channel_id:
            return False
        channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            raise TypeError(f"channel {channel_key} is not messageable")
        mentions = discord.AllowedMentions.none()
        for part in parts[:-1]:
            await channel.send(part, allowed_mentions=mentions, suppress_embeds=True)
        view = discord.ui.View(timeout=None)
        for action in fc.ACTIONS:
            view.add_item(draft_button(action, draft_id, default_locale))
        await channel.send(parts[-1], view=view, allowed_mentions=mentions, suppress_embeds=True)
        # The dynamic items answer clicks (also after a restart): drop this view from the
        # in-memory store so memory stays flat and a click is not handled twice.
        view.stop()
        return True

    async def send_draft_dry_run(channel_key: str, parts: list[str], draft_id: str) -> bool:
        log.info("intercom.draft_notification", channel=channel_key, parts=len(parts))
        return False

    client.notifier = Notifier(
        gate,
        send_dry_run if dry_run else send_discord,
        cursor_name=settings.intercom_cursor_name,
        locale=default_locale,
        send_draft=send_draft_dry_run if dry_run else send_draft_discord,
    )
    return client


def _register_draft_ui(gate: GateClient) -> Any:
    """Persistent draft buttons (``fc:<action>:<draft id>``) and their modals. Return the
    ``DynamicItem`` class: ``Intercom.setup_hook`` registers it so clicks on messages
    posted before a restart still work."""
    import discord

    async def finish(interaction: Any, call: Callable[[str, int], Awaitable[Reply]]) -> None:
        """Run a handler after the interaction was deferred, answer privately, and remove
        the buttons once the draft is settled."""
        locale = locale_of(str(interaction.locale))
        try:
            reply = await call(locale, interaction.user.id)
        except Exception as exc:
            capture(ec.INT_UNEXPECTED, exc, command="draft_review")
            reply = Reply(
                t(locale, "error.generic", message="Internal error", code=ec.INT_UNEXPECTED.code),
                ephemeral=True,
            )
        await interaction.followup.send(reply.text, ephemeral=True)
        if reply.final and interaction.message is not None:
            with contextlib.suppress(Exception):
                await interaction.message.edit(view=None)

    class CorrectModal(discord.ui.Modal):
        def __init__(self, draft_id: str, locale: str) -> None:
            super().__init__(title=t(locale, "draft.modal.correct.title")[:45])
            self.draft_id = draft_id
            values = ", ".join(fc.veracity_label(locale, v) for v in fc.VERACITIES)
            self.veracity: discord.ui.TextInput[Any] = discord.ui.TextInput(
                label=t(locale, "draft.modal.veracity.label")[:45],
                placeholder=t(locale, "draft.modal.veracity.placeholder", values=values)[:100],
                required=False,
                max_length=40,
            )
            self.note: discord.ui.TextInput[Any] = discord.ui.TextInput(
                label=t(locale, "draft.modal.note.label")[:45],
                style=discord.TextStyle.paragraph,
                required=False,
                max_length=1000,
            )
            self.reason: discord.ui.TextInput[Any] = discord.ui.TextInput(
                label=t(locale, "draft.modal.reason.label")[:45],
                style=discord.TextStyle.paragraph,
                required=False,
                max_length=500,
            )
            for item in (self.veracity, self.note, self.reason):
                self.add_item(item)

        async def on_submit(self, interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            veracity, note, reason = self.veracity.value, self.note.value, self.reason.value
            await finish(
                interaction,
                lambda loc, uid: fc.review_edit(
                    gate, uid, loc, self.draft_id, veracity, note, reason
                ),
            )

    class RejectModal(discord.ui.Modal):
        def __init__(self, draft_id: str, locale: str) -> None:
            super().__init__(title=t(locale, "draft.modal.reject.title")[:45])
            self.draft_id = draft_id
            self.reason: discord.ui.TextInput[Any] = discord.ui.TextInput(
                label=t(locale, "draft.modal.reject.reason")[:45],
                style=discord.TextStyle.paragraph,
                required=True,
                max_length=500,
            )
            self.add_item(self.reason)

        async def on_submit(self, interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            reason = self.reason.value
            await finish(
                interaction,
                lambda loc, uid: fc.review_reject(gate, uid, loc, self.draft_id, reason),
            )

    class SameGroupModal(discord.ui.Modal):
        def __init__(self, draft_id: str, locale: str) -> None:
            super().__init__(title=t(locale, "draft.modal.samegroup.title")[:45])
            self.draft_id = draft_id
            self.group: discord.ui.TextInput[Any] = discord.ui.TextInput(
                label=t(locale, "draft.modal.samegroup.group")[:45],
                required=True,
                min_length=36,
                max_length=36,
            )
            self.reason: discord.ui.TextInput[Any] = discord.ui.TextInput(
                label=t(locale, "draft.modal.reason.label")[:45],
                style=discord.TextStyle.paragraph,
                required=False,
                max_length=500,
            )
            for item in (self.group, self.reason):
                self.add_item(item)

        async def on_submit(self, interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            group, reason = self.group.value, self.reason.value
            await finish(
                interaction,
                lambda loc, uid: fc.group_merge_same(gate, uid, loc, self.draft_id, group, reason),
            )

    class SplitModal(discord.ui.Modal):
        def __init__(self, draft_id: str, locale: str) -> None:
            super().__init__(title=t(locale, "draft.modal.split.title")[:45])
            self.draft_id = draft_id
            self.claims: discord.ui.TextInput[Any] = discord.ui.TextInput(
                label=t(locale, "draft.modal.split.claims")[:45],
                style=discord.TextStyle.paragraph,
                required=True,
                max_length=1500,
            )
            self.reason: discord.ui.TextInput[Any] = discord.ui.TextInput(
                label=t(locale, "draft.modal.reason.label")[:45],
                style=discord.TextStyle.paragraph,
                required=False,
                max_length=500,
            )
            for item in (self.claims, self.reason):
                self.add_item(item)

        async def on_submit(self, interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            claims, reason = self.claims.value, self.reason.value
            await finish(
                interaction,
                lambda loc, uid: fc.group_split_draft(
                    gate, uid, loc, self.draft_id, claims, reason
                ),
            )

    class FactModal(discord.ui.Modal):
        def __init__(self, draft_id: str, locale: str) -> None:
            super().__init__(title=t(locale, "draft.modal.fact.title")[:45])
            self.draft_id = draft_id
            self.inputs: dict[str, discord.ui.TextInput[Any]] = {}
            for key, max_length in (
                ("value", 40),
                ("period", 40),
                ("url", 400),
                ("name", 200),
                ("published", 12),
            ):
                field: discord.ui.TextInput[Any] = discord.ui.TextInput(
                    label=t(locale, f"draft.modal.fact.{key}")[:45],
                    required=True,
                    max_length=max_length,
                )
                self.inputs[key] = field
                self.add_item(field)

        async def on_submit(self, interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            v = {k: f.value for k, f in self.inputs.items()}
            await finish(
                interaction,
                lambda loc, uid: fc.fact_create(
                    gate,
                    uid,
                    loc,
                    self.draft_id,
                    v["value"],
                    v["period"],
                    v["url"],
                    v["name"],
                    v["published"],
                ),
            )

    styles = {
        "validate": discord.ButtonStyle.success,
        "correct": discord.ButtonStyle.primary,
        "reject": discord.ButtonStyle.danger,
        "samegroup": discord.ButtonStyle.secondary,
        "split": discord.ButtonStyle.secondary,
        "fact": discord.ButtonStyle.secondary,
        "band": discord.ButtonStyle.secondary,
    }

    class DraftButton(
        discord.ui.DynamicItem[discord.ui.Button[Any]],
        template=(
            r"fc:(?P<action>validate|correct|reject|samegroup|split|fact|band)"
            r":(?P<draft>[0-9a-fA-F-]{36})"
        ),
    ):
        def __init__(self, action: str, draft_id: str, locale: str = "en") -> None:
            super().__init__(
                discord.ui.Button(
                    label=t(locale, f"draft.button.{action}"),
                    style=styles[action],
                    custom_id=fc.custom_id(action, draft_id),
                )
            )
            self.action = action
            self.draft_id = draft_id

        @classmethod
        async def from_custom_id(
            cls, interaction: discord.Interaction, item: Any, match: Any, /
        ) -> Any:
            return cls(match["action"], match["draft"])

        async def callback(self, interaction: discord.Interaction) -> None:
            locale = locale_of(str(interaction.locale))
            if self.action == "validate":
                await interaction.response.defer(ephemeral=True)
                await finish(
                    interaction, lambda loc, uid: fc.review_accept(gate, uid, loc, self.draft_id)
                )
            elif self.action == "band":
                await interaction.response.defer(ephemeral=True)
                await finish(
                    interaction, lambda loc, uid: fc.band_validate(gate, uid, loc, self.draft_id)
                )
            elif self.action == "correct":
                # A modal must be the first response: no defer.
                await interaction.response.send_modal(CorrectModal(self.draft_id, locale))
            elif self.action == "samegroup":
                await interaction.response.send_modal(SameGroupModal(self.draft_id, locale))
            elif self.action == "split":
                await interaction.response.send_modal(SplitModal(self.draft_id, locale))
            elif self.action == "fact":
                await interaction.response.send_modal(FactModal(self.draft_id, locale))
            else:
                await interaction.response.send_modal(RejectModal(self.draft_id, locale))

    return DraftButton


def _choices(values: type[Platform] | type[PublicRole]) -> list[Any]:
    from discord import app_commands

    return [app_commands.Choice(name=v.value.replace("_", " "), value=v.value) for v in values]


def _register_commands(tree: Any, gate: GateClient) -> None:
    import discord
    from discord import app_commands

    from tower.intercom.handlers import dossiers, incidents, usage, watchlist
    from tower.intercom.handlers import factcheck as fc_handlers
    from tower.intercom.handlers import profiles as profile_handlers

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

    # --- /factcheck ----------------------------------------------------------------
    @app_commands.command(name="factcheck", description="Request a fact-check of a post")
    @app_commands.describe(url="https://x.com/<account>/status/<id>")
    async def fc_request(interaction: discord.Interaction, url: str) -> None:
        await run(interaction, lambda loc, uid: fc_handlers.request(gate, uid, loc, url))

    # --- /profile and /level -------------------------------------------------------
    prof = app_commands.Group(name="profile", description="Processing profiles")

    @prof.command(name="show", description="Show the active processing profile")
    async def p_show(interaction: discord.Interaction) -> None:
        await run(interaction, lambda loc, uid: profile_handlers.show(gate, uid, loc))

    @prof.command(name="history", description="List the profile versions")
    async def p_history(interaction: discord.Interaction) -> None:
        await run(interaction, lambda loc, uid: profile_handlers.history(gate, uid, loc))

    @prof.command(name="set", description="Change one setting (new version, activated)")
    @app_commands.describe(
        path="Dotted path, e.g. levels.N1.e1_threshold or draft_daily_quota",
        value="New value (JSON: 0.2, true, 40)",
        reason="Why",
    )
    async def p_set(interaction: discord.Interaction, path: str, value: str, reason: str) -> None:
        await run(
            interaction,
            lambda loc, uid: profile_handlers.set_value(gate, uid, loc, path, value, reason),
        )

    @prof.command(name="rollback", description="Re-activate the previous profile version")
    async def p_rollback(interaction: discord.Interaction, reason: str) -> None:
        await run(interaction, lambda loc, uid: profile_handlers.rollback(gate, uid, loc, reason))

    lvl = app_commands.Group(name="level", description="Manual level overrides")
    target_choices = [app_commands.Choice(name=v, value=v) for v in ("content", "group", "account")]
    level_choices = [app_commands.Choice(name=v, value=v) for v in ("N0", "N1", "N2", "N3", "N4")]

    @lvl.command(name="set", description="Force a level for a content, group or account")
    @app_commands.describe(
        target="Content/group public id, <platform>:<external id>, or <platform>:@<handle>",
        hours="Duration in hours (0 < hours <= 720)",
        reason="Why",
    )
    @app_commands.choices(target_type=target_choices, level=level_choices)
    async def l_set(
        interaction: discord.Interaction,
        target_type: app_commands.Choice[str],
        target: str,
        level: app_commands.Choice[str],
        hours: float,
        reason: str,
    ) -> None:
        await run(
            interaction,
            lambda loc, uid: profile_handlers.level_set(
                gate, uid, loc, target_type.value, target, level.value, hours, reason
            ),
        )

    @lvl.command(name="list", description="List the active level overrides")
    async def l_list(interaction: discord.Interaction) -> None:
        await run(interaction, lambda loc, uid: profile_handlers.level_list(gate, uid, loc))

    @lvl.command(name="revoke", description="End a level override")
    @app_commands.describe(override_id="Override id (at least 6 characters)", reason="Why")
    async def l_revoke(interaction: discord.Interaction, override_id: str, reason: str) -> None:
        await run(
            interaction,
            lambda loc, uid: profile_handlers.level_revoke(gate, uid, loc, override_id, reason),
        )

    # --- /group and /fact ------------------------------------------------------------
    grp = app_commands.Group(name="group", description="Claim groups")

    @grp.command(name="show", description="Show a claim group")
    @app_commands.describe(group_id="Group id")
    async def g_show(interaction: discord.Interaction, group_id: str) -> None:
        await run(interaction, lambda loc, uid: fc_handlers.group_show(gate, uid, loc, group_id))

    @grp.command(name="merge", description="Merge the source group into the target group")
    @app_commands.describe(target="Group that absorbs", source="Group absorbed", reason="Why")
    async def g_merge(
        interaction: discord.Interaction, target: str, source: str, reason: str
    ) -> None:
        await run(
            interaction,
            lambda loc, uid: fc_handlers.group_merge(gate, uid, loc, target, source, reason),
        )

    @grp.command(name="split", description="Move claims into a new group")
    @app_commands.describe(group_id="Group id", claim_ids="Claim ids, comma separated")
    async def g_split(
        interaction: discord.Interaction, group_id: str, claim_ids: str, reason: str
    ) -> None:
        await run(
            interaction,
            lambda loc, uid: fc_handlers.group_split(gate, uid, loc, group_id, claim_ids, reason),
        )

    fct = app_commands.Group(name="fact", description="Reference facts and value bands")
    status_choices = [
        app_commands.Choice(name=v, value=v) for v in ("pending", "validated", "rejected")
    ]

    @fct.command(name="show", description="Show a reference fact")
    @app_commands.describe(fact_id="Fact id (at least 6 characters)")
    async def f_show(interaction: discord.Interaction, fact_id: str) -> None:
        await run(interaction, lambda loc, uid: fc_handlers.fact_show(gate, uid, loc, fact_id))

    @fct.command(name="list", description="List reference facts")
    @app_commands.choices(status=status_choices)
    async def f_list(
        interaction: discord.Interaction, status: app_commands.Choice[str] | None = None
    ) -> None:
        value = status.value if status else None
        await run(interaction, lambda loc, uid: fc_handlers.fact_list(gate, uid, loc, value))

    @fct.command(name="validate", description="Validate a pending reference fact")
    async def f_validate(interaction: discord.Interaction, fact_id: str) -> None:
        await run(interaction, lambda loc, uid: fc_handlers.fact_validate(gate, uid, loc, fact_id))

    @fct.command(name="reject", description="Reject a pending reference fact")
    async def f_reject(interaction: discord.Interaction, fact_id: str, reason: str) -> None:
        await run(
            interaction, lambda loc, uid: fc_handlers.fact_reject(gate, uid, loc, fact_id, reason)
        )

    @fct.command(name="band", description="Validate the value band of an active verdict")
    @app_commands.describe(verdict_id="Verdict (draft) id")
    async def f_band(interaction: discord.Interaction, verdict_id: str) -> None:
        await run(
            interaction, lambda loc, uid: fc_handlers.band_validate(gate, uid, loc, verdict_id)
        )

    for group in (wl, dos, inc, prof, lvl, grp, fct):
        tree.add_command(group)
    tree.add_command(u_show)
    tree.add_command(fc_request)
