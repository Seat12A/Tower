"""``/permissions`` and the persistent controls of the permissions panel.

Authorization model: gate decides (it re-checks every write, including anti-escalation);
the bot adds the Discord role hierarchy and offers a select only for what the actor may
change. A submitted select is re-filtered server-side: a custom id can be forged.
"""

from __future__ import annotations

import re
from typing import Any

import discord
from discord import app_commands, ui

from tower.intercom.commands import Context
from tower.intercom.commands.team import actor_top, edit_card, selected_values, target_top
from tower.intercom.handlers import team as h
from tower.intercom.ui.base import guarded_callback, run_handler

SAMPLE_CUSTOM_IDS = (
    h.cid_perm_set(123456789012345678, "cases"),
    h.cid_perm_clear("ask", 123456789012345678),
)

_CATEGORY_IDS = "|".join(c.id for c in h.CATEGORIES)


def dynamic_items(ctx: Context) -> list[type]:
    gate = ctx.gate

    class PermissionSelect(
        ui.DynamicItem[ui.Select[Any]],
        template=rf"t12a:perm:set:(?P<target>\d+):(?P<category>{_CATEGORY_IDS})",
    ):
        """One category of the panel: new = (current - options) + selected."""

        def __init__(self, target: int, category: str) -> None:
            super().__init__(
                ui.Select(
                    custom_id=h.cid_perm_set(target, category),
                    min_values=0,
                    options=[discord.SelectOption(label="-", value="-")],
                )
            )
            self.target = target
            self.category = category

        @classmethod
        async def from_custom_id(
            cls, interaction: discord.Interaction, item: ui.Item[Any], match: re.Match[str], /
        ) -> PermissionSelect:
            return cls(int(match["target"]), match["category"])

        @guarded_callback
        async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
            selected = selected_values(interaction)
            target, category = self.target, self.category
            top = await target_top(interaction, target)
            mine = actor_top(interaction)
            await edit_card(
                interaction,
                lambda loc, uid: h.set_category(
                    gate, uid, loc, target, category, selected, mine, top
                ),
            )

    class PermissionButton(
        ui.DynamicItem[ui.Button[Any]],
        template=r"t12a:perm:clear_(?P<action>ask|yes|no):(?P<target>\d+)",
    ):
        """ "Remove all" with its in-place confirmation."""

        def __init__(self, action: str, target: int) -> None:
            super().__init__(ui.Button(custom_id=h.cid_perm_clear(action, target), label="-"))
            self.action = action
            self.target = target

        @classmethod
        async def from_custom_id(
            cls, interaction: discord.Interaction, item: ui.Item[Any], match: re.Match[str], /
        ) -> PermissionButton:
            return cls(match["action"], int(match["target"]))

        @guarded_callback
        async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
            target, action = self.target, self.action
            top = await target_top(interaction, target)
            mine = actor_top(interaction)
            if action == "yes":
                await edit_card(
                    interaction,
                    lambda loc, uid: h.clear_permissions(gate, uid, loc, target, mine, top),
                )
            else:
                await edit_card(
                    interaction,
                    lambda loc, uid: h.permissions_panel(
                        gate, uid, loc, target, mine, top, confirm_clear=action == "ask"
                    ),
                )

    return [PermissionSelect, PermissionButton]


def register(tree: app_commands.CommandTree[Any], ctx: Context) -> None:
    gate = ctx.gate

    @tree.command(name="permissions", description="See or edit the permissions of a member")
    @app_commands.describe(member="Team member (default: yourself)")
    @app_commands.guild_only()
    async def permissions(
        interaction: discord.Interaction, member: discord.Member | None = None
    ) -> None:
        target = member or interaction.user
        top = target.top_role.position if isinstance(target, discord.Member) else None
        await run_handler(
            interaction,
            lambda loc, uid: h.permissions_panel(
                gate, uid, loc, target.id, actor_top(interaction), top
            ),
        )
