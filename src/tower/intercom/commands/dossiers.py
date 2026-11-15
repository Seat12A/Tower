"""``/dossier``: dossier card with persistent action buttons (``t12a:dos:<action>:<id>``).

take / validate re-fetch the dossier and refresh the card in place; reject and publish open
a modal (note / publication link). Publication on X stays manual.
"""

import functools
import re
from typing import Any, Self

import discord
from discord import app_commands, ui

from tower.intercom.commands import Context
from tower.intercom.handlers import dossiers as dos
from tower.intercom.i18n import t
from tower.intercom.ui.base import BaseModal, guarded_callback, interaction_locale, run_handler

SAMPLE_CUSTOM_IDS = ("t12a:dos:take:00000000-0000-0000-0000-000000000000",)

_BUTTON = r"t12a:dos:(?P<action>take|validate|reject|publish):(?P<public_id>[0-9A-Za-z-]{6,64})"


@functools.cache
def _kit(ctx: Context) -> tuple[Any, Any, Any]:
    gate = ctx.gate

    class RejectModal(BaseModal):
        def __init__(self, public_id: str, locale: str) -> None:
            super().__init__(title=t(locale, "dossier.modal.reject.title")[:45])
            self.public_id = public_id
            self.note: ui.TextInput[Any] = ui.TextInput(
                style=discord.TextStyle.paragraph, required=False, max_length=1000
            )
            self.add_item(
                ui.Label(
                    text=t(locale, "dossier.modal.note")[:45],
                    description=t(locale, "dossier.modal.note.help")[:100],
                    component=self.note,
                )
            )

        async def on_submit(self, interaction: discord.Interaction) -> None:
            await run_handler(
                interaction,
                lambda loc, uid: dos.reject(gate, uid, loc, self.public_id, self.note.value),
                edit=interaction.message is not None,
            )

    class PublishModal(BaseModal):
        def __init__(self, public_id: str, locale: str) -> None:
            super().__init__(title=t(locale, "dossier.modal.publish.title")[:45])
            self.public_id = public_id
            self.url: ui.TextInput[Any] = ui.TextInput(
                max_length=400, placeholder="https://x.com/<account>/status/<id>"
            )
            self.add_item(ui.TextDisplay(f"-# {t(locale, 'dossier.modal.publish.help')}"))
            self.add_item(ui.Label(text=t(locale, "dossier.modal.url")[:45], component=self.url))

        async def on_submit(self, interaction: discord.Interaction) -> None:
            await run_handler(
                interaction,
                lambda loc, uid: dos.publish(gate, uid, loc, self.public_id, self.url.value),
                edit=interaction.message is not None,
            )

    class DossierButton(ui.DynamicItem[ui.Button[Any]], template=_BUTTON):
        def __init__(self, custom_id: str) -> None:
            super().__init__(ui.Button(custom_id=custom_id))

        @classmethod
        async def from_custom_id(
            cls, interaction: discord.Interaction, item: ui.Item[Any], match: re.Match[str], /
        ) -> Self:
            return cls(match.string)

        @guarded_callback
        async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
            _, _, action, public_id = (self.item.custom_id or "").split(":", 3)
            locale = interaction_locale(interaction)
            if action == "reject":
                await interaction.response.send_modal(RejectModal(public_id, locale))
            elif action == "publish":
                await interaction.response.send_modal(PublishModal(public_id, locale))
            elif action == "take":
                await run_handler(
                    interaction, lambda loc, uid: dos.take(gate, uid, loc, public_id), edit=True
                )
            else:
                await run_handler(
                    interaction,
                    lambda loc, uid: dos.validate(gate, uid, loc, public_id),
                    edit=True,
                )

    return DossierButton, RejectModal, PublishModal


def dynamic_items(ctx: Context) -> list[type]:
    return [_kit(ctx)[0]]


def register(tree: Any, ctx: Context) -> None:
    gate = ctx.gate
    _, reject_modal, publish_modal = _kit(ctx)
    group = app_commands.Group(name="dossier", description="Fact-check dossiers", guild_only=True)

    @group.command(name="show", description="Show a dossier with its action buttons")
    @app_commands.describe(public_id="Dossier public id")
    async def d_show(interaction: discord.Interaction, public_id: str) -> None:
        await run_handler(interaction, lambda loc, uid: dos.show(gate, uid, loc, public_id))

    @group.command(name="take", description="Take a dossier into review")
    @app_commands.describe(public_id="Dossier public id")
    async def d_take(interaction: discord.Interaction, public_id: str) -> None:
        await run_handler(interaction, lambda loc, uid: dos.take(gate, uid, loc, public_id))

    @group.command(name="validate", description="Validate a dossier")
    @app_commands.describe(public_id="Dossier public id", note="Optional note")
    async def d_validate(
        interaction: discord.Interaction, public_id: str, note: str | None = None
    ) -> None:
        await run_handler(
            interaction, lambda loc, uid: dos.validate(gate, uid, loc, public_id, note)
        )

    @group.command(name="reject", description="Reject a dossier (opens a note form)")
    @app_commands.describe(public_id="Dossier public id")
    async def d_reject(interaction: discord.Interaction, public_id: str) -> None:
        await interaction.response.send_modal(
            reject_modal(public_id, interaction_locale(interaction))
        )

    @group.command(name="publish", description="Record the X publication link (manual post)")
    @app_commands.describe(public_id="Dossier public id")
    async def d_publish(interaction: discord.Interaction, public_id: str) -> None:
        await interaction.response.send_modal(
            publish_modal(public_id, interaction_locale(interaction))
        )

    tree.add_command(group)
