"""``/registry``: entry card with persistent buttons (``t12a:reg:<action>:<domain>``).

``tier`` and ``control`` open a form (tier select / controlling entity, plus the editorial
reason); ``show``, ``history`` and ``seed`` call gate directly. A domain too long for a
custom id simply gets no buttons (the slash commands still work).
"""

import functools
import re
from typing import Any, Self

import discord
from discord import app_commands, ui

from tower.intercom.commands import Context
from tower.intercom.handlers import registry as rh
from tower.intercom.i18n import t
from tower.intercom.ui.base import BaseModal, guarded_callback, interaction_locale, run_handler

SAMPLE_CUSTOM_IDS = ("t12a:reg:show:example.org",)

_BUTTON = r"t12a:reg:(?P<action>show|tier|control|history|seed):(?P<domain>[0-9a-z.-]{1,80})"


def _reason() -> ui.TextInput[Any]:
    return ui.TextInput(style=discord.TextStyle.paragraph, max_length=1000)


@functools.cache
def _kit(ctx: Context) -> tuple[Any, Any, Any]:
    gate = ctx.gate

    class TierModal(BaseModal):
        def __init__(self, domain: str, locale: str) -> None:
            super().__init__(title=t(locale, "registry.modal.tier.title")[:45])
            self.domain = domain
            self.tier: ui.Select[Any] = ui.Select(
                options=[
                    discord.SelectOption(
                        label=tier,
                        value=tier,
                        description=t(locale, f"registry.tier.{tier}")[:100],
                    )
                    for tier in rh.TIERS
                ]
            )
            self.reason = _reason()
            self.add_item(ui.TextDisplay(f"-# {t(locale, 'registry.modal.help', domain=domain)}"))
            self.add_item(ui.Label(text=t(locale, "registry.modal.tier")[:45], component=self.tier))
            self.add_item(
                ui.Label(
                    text=t(locale, "registry.modal.reason")[:45],
                    description=t(locale, "registry.modal.reason.help")[:100],
                    component=self.reason,
                )
            )

        async def on_submit(self, interaction: discord.Interaction) -> None:
            tier = self.tier.values[0] if self.tier.values else rh.TIERS[0]
            await run_handler(
                interaction,
                lambda loc, uid: rh.registry_set_tier(
                    gate, uid, loc, self.domain, tier, self.reason.value
                ),
                edit=interaction.message is not None,
            )

    class ControlModal(BaseModal):
        def __init__(self, domain: str, locale: str) -> None:
            super().__init__(title=t(locale, "registry.modal.control.title")[:45])
            self.domain = domain
            self.controlled_by: ui.TextInput[Any] = ui.TextInput(max_length=200)
            self.reason = _reason()
            self.add_item(ui.TextDisplay(f"-# {t(locale, 'registry.modal.help', domain=domain)}"))
            self.add_item(
                ui.Label(
                    text=t(locale, "registry.modal.controlled_by")[:45],
                    description=t(locale, "registry.modal.controlled_by.help")[:100],
                    component=self.controlled_by,
                )
            )
            self.add_item(
                ui.Label(
                    text=t(locale, "registry.modal.reason")[:45],
                    description=t(locale, "registry.modal.reason.help")[:100],
                    component=self.reason,
                )
            )

        async def on_submit(self, interaction: discord.Interaction) -> None:
            await run_handler(
                interaction,
                lambda loc, uid: rh.registry_set_control(
                    gate, uid, loc, self.domain, self.controlled_by.value, self.reason.value
                ),
                edit=interaction.message is not None,
            )

    class RegistryButton(ui.DynamicItem[ui.Button[Any]], template=_BUTTON):
        def __init__(self, custom_id: str) -> None:
            super().__init__(ui.Button(custom_id=custom_id))

        @classmethod
        async def from_custom_id(
            cls, interaction: discord.Interaction, item: ui.Item[Any], match: re.Match[str], /
        ) -> Self:
            return cls(match.string)

        @guarded_callback
        async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
            _, _, action, domain = (self.item.custom_id or "").split(":", 3)
            locale = interaction_locale(interaction)
            if action == "tier":
                await interaction.response.send_modal(TierModal(domain, locale))
            elif action == "control":
                await interaction.response.send_modal(ControlModal(domain, locale))
            elif action == "history":
                await run_handler(
                    interaction,
                    lambda loc, uid: rh.registry_history(gate, uid, loc, domain),
                    edit=True,
                )
            elif action == "seed":
                await run_handler(
                    interaction,
                    lambda loc, uid: rh.registry_seed_control(gate, uid, loc, domain),
                    edit=True,
                )
            else:
                await run_handler(
                    interaction,
                    lambda loc, uid: rh.registry_show(gate, uid, loc, domain),
                    edit=True,
                )

    return RegistryButton, TierModal, ControlModal


def dynamic_items(ctx: Context) -> list[type]:
    return [_kit(ctx)[0]]


def register(tree: Any, ctx: Context) -> None:
    gate = ctx.gate
    _, tier_modal, control_modal = _kit(ctx)
    group = app_commands.Group(
        name="registry", description="Source reliability registry", guild_only=True
    )

    @group.command(name="show", description="Show the registry entry of a domain")
    @app_commands.describe(domain="Domain (e.g. insee.fr)")
    async def r_show(interaction: discord.Interaction, domain: str) -> None:
        await run_handler(interaction, lambda loc, uid: rh.registry_show(gate, uid, loc, domain))

    @group.command(name="set-tier", description="Change the tier of a source (editors)")
    @app_commands.describe(domain="Domain (e.g. insee.fr)")
    async def r_set_tier(interaction: discord.Interaction, domain: str) -> None:
        await interaction.response.send_modal(
            tier_modal(rh.normalize_domain(domain), interaction_locale(interaction))
        )

    @group.command(name="set-control", description="Set or clear the controlling entity (editors)")
    @app_commands.describe(domain="Domain (e.g. insee.fr)")
    async def r_set_control(interaction: discord.Interaction, domain: str) -> None:
        await interaction.response.send_modal(
            control_modal(rh.normalize_domain(domain), interaction_locale(interaction))
        )

    @group.command(name="history", description="Version history of a registry entry")
    @app_commands.describe(domain="Domain (e.g. insee.fr)")
    async def r_history(interaction: discord.Interaction, domain: str) -> None:
        await run_handler(interaction, lambda loc, uid: rh.registry_history(gate, uid, loc, domain))

    @group.command(name="seed-control", description="Seed the controller from Wikidata (editors)")
    @app_commands.describe(domain="Domain (e.g. insee.fr)")
    async def r_seed_control(interaction: discord.Interaction, domain: str) -> None:
        await run_handler(
            interaction, lambda loc, uid: rh.registry_seed_control(gate, uid, loc, domain)
        )

    tree.add_command(group)
