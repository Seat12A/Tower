"""``/watchlist``: paged list with a platform filter, account card, add/edit modals.

Persistent components (``t12a:wl:...``):

- buttons ``page:<platform|all>:<page>``, ``add``, ``show:<id>``, ``edit:<id>``,
  ``off:<id>`` (confirmation card), ``offyes:<id>``;
- selects ``plat`` (platform filter) and ``open`` (open an account of the page).

Every callback re-fetches from gate and updates the card in place.
"""

import functools
import re
from typing import Any, Self, cast

import discord
from discord import app_commands, ui

from tower.core.domain.platforms import Platform, PublicRole
from tower.intercom.commands import Context
from tower.intercom.handlers import watchlist as wl
from tower.intercom.i18n import t
from tower.intercom.ui.base import BaseModal, guarded_callback, interaction_locale, run_handler

PREFIX = "t12a:wl:"
CATEGORIES = tuple(r.value for r in PublicRole)

SAMPLE_CUSTOM_IDS = (
    "t12a:wl:page:all:0",
    "t12a:wl:add",
    "t12a:wl:edit:00000000-0000-0000-0000-000000000000",
    "t12a:wl:plat",
)
"""One custom id per dynamic item class."""

_BUTTON = r"t12a:wl:(?P<action>page|add|show|edit|off|offyes)(?::(?P<params>[0-9A-Za-z_:.-]*))?"
_SELECT = r"t12a:wl:(?P<action>plat|open)"


def _params(custom_id: str) -> tuple[str, list[str]]:
    _, _, action, *params = custom_id.split(":")
    return action, params


def _label(key: str, locale: str) -> str:
    return t(locale, key)[:45]


def _text_input(**kwargs: Any) -> ui.TextInput[Any]:
    return ui.TextInput(**kwargs)


@functools.cache
def _kit(ctx: Context) -> tuple[Any, Any, Any, Any]:
    """Dynamic items and modals bound to the gate of ``ctx`` (built once per context)."""
    gate = ctx.gate

    def choose(values: list[str]) -> str | None:
        return values[0] if values else None

    class AddModal(BaseModal):
        def __init__(self, locale: str) -> None:
            super().__init__(title=_label("watchlist.modal.add.title", locale))
            self.platform: ui.Select[Any] = ui.Select(
                options=[
                    discord.SelectOption(label=p, value=p, default=p == Platform.X.value)
                    for p in wl.PLATFORMS
                ]
            )
            self.category: ui.Select[Any] = ui.Select(
                options=[
                    discord.SelectOption(label=c.replace("_", " "), value=c) for c in CATEGORIES
                ]
            )
            self.handle = _text_input(max_length=100, placeholder="example_account")
            self.priority = _text_input(required=False, default="0", max_length=4)
            self.note = _text_input(
                style=discord.TextStyle.paragraph, required=False, max_length=500
            )
            for key, component in (
                ("platform", self.platform),
                ("category", self.category),
                ("handle", self.handle),
                ("priority", self.priority),
                ("note", self.note),
            ):
                self.add_item(
                    ui.Label(
                        text=_label(f"watchlist.modal.{key}", locale),
                        description=t(locale, f"watchlist.modal.{key}.help")[:100] or None,
                        component=component,
                    )
                )

        async def on_submit(self, interaction: discord.Interaction) -> None:
            platform = choose(self.platform.values) or Platform.X.value
            category = choose(self.category.values) or CATEGORIES[0]
            await run_handler(
                interaction,
                lambda loc, uid: wl.add_form(
                    gate,
                    uid,
                    loc,
                    platform,
                    self.handle.value,
                    category,
                    self.priority.value,
                    self.note.value or None,
                ),
                edit=interaction.message is not None,
            )

    class EditModal(BaseModal):
        def __init__(self, public_id: str, locale: str) -> None:
            super().__init__(title=_label("watchlist.modal.edit.title", locale))
            self.public_id = public_id
            self.category: ui.Select[Any] = ui.Select(
                required=False,
                min_values=0,
                options=[
                    discord.SelectOption(label=c.replace("_", " "), value=c) for c in CATEGORIES
                ],
            )
            self.handle = _text_input(required=False, max_length=100)
            self.priority = _text_input(required=False, max_length=4)
            self.note = _text_input(
                style=discord.TextStyle.paragraph, required=False, max_length=500
            )
            self.add_item(ui.TextDisplay(f"-# {t(locale, 'watchlist.modal.edit.help')}"))
            for key, component in (
                ("category", self.category),
                ("handle", self.handle),
                ("priority", self.priority),
                ("note", self.note),
            ):
                self.add_item(
                    ui.Label(
                        text=_label(f"watchlist.modal.{key}", locale),
                        description=t(locale, f"watchlist.modal.{key}.help")[:100] or None,
                        component=component,
                    )
                )

        async def on_submit(self, interaction: discord.Interaction) -> None:
            category = choose(self.category.values)
            await run_handler(
                interaction,
                lambda loc, uid: wl.edit_form(
                    gate,
                    uid,
                    loc,
                    self.public_id,
                    category,
                    self.priority.value,
                    self.note.value,
                    self.handle.value,
                ),
                edit=interaction.message is not None,
            )

    class WatchButton(ui.DynamicItem[ui.Button[Any]], template=_BUTTON):
        def __init__(self, custom_id: str) -> None:
            super().__init__(ui.Button(custom_id=custom_id))

        @classmethod
        async def from_custom_id(
            cls, interaction: discord.Interaction, item: ui.Item[Any], match: re.Match[str], /
        ) -> Self:
            return cls(match.string)

        @guarded_callback
        async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
            action, params = _params(self.item.custom_id or "")
            locale = interaction_locale(interaction)
            ref = params[0] if params else ""
            if action == "add":
                await interaction.response.send_modal(AddModal(locale))
            elif action == "edit":
                await interaction.response.send_modal(EditModal(ref, locale))
            elif action == "page":
                platform = None if ref == wl.ALL else ref
                page = int(params[1]) if len(params) > 1 and params[1].lstrip("-").isdigit() else 0
                await run_handler(
                    interaction,
                    lambda loc, uid: wl.list_accounts(gate, uid, loc, platform, page),
                    edit=True,
                )
            elif action == "show":
                await run_handler(
                    interaction, lambda loc, uid: wl.show_account(gate, uid, loc, ref), edit=True
                )
            elif action == "off":
                await run_handler(
                    interaction,
                    lambda loc, uid: wl.confirm_deactivate(gate, uid, loc, ref),
                    edit=True,
                )
            else:  # offyes
                await run_handler(
                    interaction, lambda loc, uid: wl.deactivate(gate, uid, loc, ref), edit=True
                )

    class WatchSelect(ui.DynamicItem[ui.Select[Any]], template=_SELECT):
        def __init__(self, custom_id: str) -> None:
            super().__init__(ui.Select(custom_id=custom_id))

        @classmethod
        async def from_custom_id(
            cls, interaction: discord.Interaction, item: ui.Item[Any], match: re.Match[str], /
        ) -> Self:
            return cls(match.string)

        @guarded_callback
        async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
            action, _ = _params(self.item.custom_id or "")
            values: list[str] = list(cast(dict[str, Any], interaction.data or {}).get("values", []))
            value = values[0] if values else wl.ALL
            if action == "plat":
                platform = None if value == wl.ALL else value
                await run_handler(
                    interaction,
                    lambda loc, uid: wl.list_accounts(gate, uid, loc, platform, 0),
                    edit=True,
                )
            else:  # open
                await run_handler(
                    interaction, lambda loc, uid: wl.show_account(gate, uid, loc, value), edit=True
                )

    return WatchButton, WatchSelect, AddModal, EditModal


def dynamic_items(ctx: Context) -> list[type]:
    button, select = _kit(ctx)[:2]
    return [button, select]


def register(tree: Any, ctx: Context) -> None:
    gate = ctx.gate
    _, _, add_modal, edit_modal = _kit(ctx)
    group = app_commands.Group(name="watchlist", description="Watched accounts", guild_only=True)
    platform_choices = [app_commands.Choice(name=p, value=p) for p in wl.PLATFORMS]

    @group.command(name="list", description="List watched accounts")
    @app_commands.describe(platform="Only this platform")
    @app_commands.choices(platform=platform_choices)
    async def wl_list(
        interaction: discord.Interaction, platform: app_commands.Choice[str] | None = None
    ) -> None:
        value = platform.value if platform else None
        await run_handler(interaction, lambda loc, uid: wl.list_accounts(gate, uid, loc, value, 0))

    @group.command(name="add", description="Add an account by handle (resolved by the radar)")
    async def wl_add(interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(add_modal(interaction_locale(interaction)))

    @group.command(name="edit", description="Edit a watched account")
    @app_commands.describe(public_id="Public id (at least 6 characters)")
    async def wl_edit(interaction: discord.Interaction, public_id: str) -> None:
        await interaction.response.send_modal(
            edit_modal(public_id, interaction_locale(interaction))
        )

    @group.command(name="deactivate", description="Stop watching an account")
    @app_commands.describe(public_id="Public id (at least 6 characters)")
    async def wl_deactivate(interaction: discord.Interaction, public_id: str) -> None:
        await run_handler(
            interaction, lambda loc, uid: wl.confirm_deactivate(gate, uid, loc, public_id)
        )

    tree.add_command(group)
