"""``/incidents``: list with a show select (``t12a:inc:show``), a resolve select
(``t12a:inc:pick``) and a refresh button (``t12a:inc:list``); ``show`` card with a resolve
button (``t12a:inc:res:<fingerprint>``). Resolving opens a note form."""

import functools
import re
from typing import Any, Self, cast

import discord
from discord import app_commands, ui

from tower.intercom.commands import Context
from tower.intercom.handlers import incidents as inc
from tower.intercom.i18n import t
from tower.intercom.ui.base import BaseModal, guarded_callback, interaction_locale, run_handler

SAMPLE_CUSTOM_IDS = (
    inc.CID_LIST,
    inc.CID_PICK,
    inc.CID_SHOW,
    inc.CID_RESOLVE_PREFIX + "0123456789abcdef",
)

_BUTTON = re.escape(inc.CID_LIST)
_SELECT = re.escape(inc.CID_PICK)
_SHOW = re.escape(inc.CID_SHOW)
_RESOLVE = re.escape(inc.CID_RESOLVE_PREFIX) + r"(?P<fp>[0-9a-f]{6,64})"


@functools.cache
def _kit(ctx: Context) -> tuple[Any, Any, Any, Any, Any]:
    gate = ctx.gate

    class ResolveModal(BaseModal):
        def __init__(self, fingerprint: str, locale: str) -> None:
            super().__init__(title=t(locale, "incidents.modal.title")[:45])
            self.fingerprint = fingerprint
            self.note: ui.TextInput[Any] = ui.TextInput(
                style=discord.TextStyle.paragraph, required=False, max_length=1000
            )
            self.add_item(
                ui.Label(
                    text=t(locale, "incidents.modal.note")[:45],
                    description=t(locale, "incidents.modal.note.help")[:100],
                    component=self.note,
                )
            )

        async def on_submit(self, interaction: discord.Interaction) -> None:
            await run_handler(
                interaction,
                lambda loc, uid: inc.resolve(gate, uid, loc, self.fingerprint, self.note.value),
                edit=interaction.message is not None,
            )

    class ListButton(ui.DynamicItem[ui.Button[Any]], template=_BUTTON):
        def __init__(self) -> None:
            super().__init__(ui.Button(custom_id=inc.CID_LIST))

        @classmethod
        async def from_custom_id(
            cls, interaction: discord.Interaction, item: ui.Item[Any], match: re.Match[str], /
        ) -> Self:
            return cls()

        @guarded_callback
        async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
            await run_handler(
                interaction, lambda loc, uid: inc.list_incidents(gate, uid, loc), edit=True
            )

    class PickSelect(ui.DynamicItem[ui.Select[Any]], template=_SELECT):
        def __init__(self) -> None:
            super().__init__(ui.Select(custom_id=inc.CID_PICK))

        @classmethod
        async def from_custom_id(
            cls, interaction: discord.Interaction, item: ui.Item[Any], match: re.Match[str], /
        ) -> Self:
            return cls()

        @guarded_callback
        async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
            values = list(cast(dict[str, Any], interaction.data or {}).get("values", []))
            if values:
                await interaction.response.send_modal(
                    ResolveModal(str(values[0]), interaction_locale(interaction))
                )

    class ShowSelect(ui.DynamicItem[ui.Select[Any]], template=_SHOW):
        def __init__(self) -> None:
            super().__init__(ui.Select(custom_id=inc.CID_SHOW))

        @classmethod
        async def from_custom_id(
            cls, interaction: discord.Interaction, item: ui.Item[Any], match: re.Match[str], /
        ) -> Self:
            return cls()

        @guarded_callback
        async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
            values = list(cast(dict[str, Any], interaction.data or {}).get("values", []))
            if values:
                fp = str(values[0])
                await run_handler(interaction, lambda loc, uid: inc.show(gate, uid, loc, fp))

    class ResolveButton(ui.DynamicItem[ui.Button[Any]], template=_RESOLVE):
        def __init__(self, fingerprint: str) -> None:
            super().__init__(ui.Button(custom_id=inc.CID_RESOLVE_PREFIX + fingerprint))
            self.fingerprint = fingerprint

        @classmethod
        async def from_custom_id(
            cls, interaction: discord.Interaction, item: ui.Item[Any], match: re.Match[str], /
        ) -> Self:
            return cls(match["fp"])

        @guarded_callback
        async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
            await interaction.response.send_modal(
                ResolveModal(self.fingerprint, interaction_locale(interaction))
            )

    return ListButton, PickSelect, ShowSelect, ResolveButton, ResolveModal


def dynamic_items(ctx: Context) -> list[type]:
    return list(_kit(ctx)[:4])


def register(tree: Any, ctx: Context) -> None:
    gate = ctx.gate
    resolve_modal = _kit(ctx)[4]
    group = app_commands.Group(name="incidents", description="Stored errors", guild_only=True)

    @group.command(name="list", description="List open incidents")
    @app_commands.describe(
        code="Only this error code (e.g. TWR-FCK-019)",
        all="Include resolved incidents",
    )
    async def i_list(
        interaction: discord.Interaction, code: str | None = None, all: bool = False
    ) -> None:
        await run_handler(
            interaction,
            lambda loc, uid: inc.list_incidents(
                gate, uid, loc, code_filter=code, include_resolved=all
            ),
        )

    @group.command(name="show", description="Details of an incident (message, context, trace)")
    @app_commands.describe(fingerprint="Fingerprint (at least 6 characters)")
    async def i_show(interaction: discord.Interaction, fingerprint: str) -> None:
        await run_handler(interaction, lambda loc, uid: inc.show(gate, uid, loc, fingerprint))

    @group.command(name="resolve", description="Resolve an incident (opens a note form)")
    @app_commands.describe(fingerprint="Fingerprint (at least 6 characters)")
    async def i_resolve(interaction: discord.Interaction, fingerprint: str) -> None:
        await interaction.response.send_modal(
            resolve_modal(fingerprint, interaction_locale(interaction))
        )

    tree.add_command(group)
