"""``/team``, ``/rank`` and ``/unrank`` plus the persistent components of the team area.

Authorization model: gate decides permissions on every call; the bot only adds the Discord
role hierarchy (positions are re-derived from the interaction at each click, never stored).
"""

from __future__ import annotations

import re
from typing import Any

import discord
from discord import app_commands, ui

from tower.intercom.commands import Context
from tower.intercom.handlers import team as h
from tower.intercom.ui.base import (
    HandlerCall,
    guarded_callback,
    run_handler,
)

SAMPLE_CUSTOM_IDS = (
    h.CID_TEAM_OPEN,
    h.cid_perms(123456789012345678),
)


def actor_top(interaction: discord.Interaction) -> int | None:
    user = interaction.user
    return user.top_role.position if isinstance(user, discord.Member) else None


async def target_top(interaction: discord.Interaction, target_id: int) -> int | None:
    """Highest role position of ``target_id`` in the guild (REST fetch: the bot has no
    members intent); ``None`` when the user is not in the guild."""
    if target_id == interaction.user.id or interaction.guild is None:
        return actor_top(interaction)
    try:
        member = await interaction.guild.fetch_member(target_id)
    except discord.NotFound:
        return None
    return member.top_role.position


async def edit_card(interaction: discord.Interaction, call: HandlerCall) -> None:
    """Defer, run the pure handler and replace the clicked message with the card."""
    await run_handler(interaction, call, edit=True)


def selected_values(interaction: discord.Interaction) -> list[str]:
    data: Any = interaction.data or {}
    return [str(v) for v in data.get("values", [])]


def dynamic_items(ctx: Context) -> list[type]:
    gate = ctx.gate

    class TeamOpenSelect(ui.DynamicItem[ui.Select[Any]], template=re.escape(h.CID_TEAM_OPEN)):
        """Opens the permissions panel of the chosen member. Auth: gate + hierarchy."""

        def __init__(self) -> None:
            super().__init__(
                ui.Select(
                    custom_id=h.CID_TEAM_OPEN,
                    options=[discord.SelectOption(label="-", value="-")],
                )
            )

        @classmethod
        async def from_custom_id(
            cls, interaction: discord.Interaction, item: ui.Item[Any], match: re.Match[str], /
        ) -> TeamOpenSelect:
            return cls()

        @guarded_callback
        async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
            values = selected_values(interaction)
            if not values or not values[0].isdigit():
                return
            target = int(values[0])
            top = await target_top(interaction, target)
            await edit_card(
                interaction,
                lambda loc, uid: h.permissions_panel(
                    gate, uid, loc, target, actor_top(interaction), top
                ),
            )

    class TeamButton(
        ui.DynamicItem[ui.Button[Any]],
        template=r"t12a:team:(?P<action>list|perms|unrank_yes|unrank_no)(?::(?P<target>\d+))?",
    ):
        """Back to the list, open a panel, confirm or cancel ``/unrank``."""

        def __init__(self, action: str, target: int | None) -> None:
            suffix = f":{target}" if target is not None else ""
            super().__init__(ui.Button(custom_id=f"t12a:team:{action}{suffix}", label="-"))
            self.action = action
            self.target = target

        @classmethod
        async def from_custom_id(
            cls, interaction: discord.Interaction, item: ui.Item[Any], match: re.Match[str], /
        ) -> TeamButton:
            target = match["target"]
            return cls(match["action"], int(target) if target else None)

        @guarded_callback
        async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
            target = self.target
            if self.action == "list" or target is None:
                await edit_card(interaction, lambda loc, uid: h.team_list(gate, uid, loc))
                return
            if self.action == "unrank_no":
                await edit_card(interaction, lambda loc, uid: h.unrank_cancelled(gate, uid, loc))
                return
            top = await target_top(interaction, target)
            mine = actor_top(interaction)
            if self.action == "perms":
                await edit_card(
                    interaction,
                    lambda loc, uid: h.permissions_panel(gate, uid, loc, target, mine, top),
                )
            else:  # unrank_yes
                await edit_card(
                    interaction,
                    lambda loc, uid: h.unrank_do(gate, uid, loc, target, mine, top),
                )

    return [TeamOpenSelect, TeamButton]


def register(tree: app_commands.CommandTree[Any], ctx: Context) -> None:
    gate = ctx.gate

    @tree.command(name="team", description="List the ranked team members")
    @app_commands.guild_only()
    async def team(interaction: discord.Interaction) -> None:
        await run_handler(interaction, lambda loc, uid: h.team_list(gate, uid, loc))

    @tree.command(name="rank", description="Add a member to the team")
    @app_commands.describe(member="Discord member to rank")
    @app_commands.guild_only()
    async def rank(interaction: discord.Interaction, member: discord.Member) -> None:
        await run_handler(
            interaction,
            lambda loc, uid: h.rank(
                gate,
                uid,
                loc,
                member.id,
                member.display_name,
                actor_top(interaction),
                member.top_role.position,
            ),
        )

    @tree.command(name="unrank", description="Remove a member from the team")
    @app_commands.describe(member="Team member to remove")
    @app_commands.guild_only()
    async def unrank(interaction: discord.Interaction, member: discord.Member) -> None:
        await run_handler(
            interaction,
            lambda loc, uid: h.unrank_confirm(
                gate, uid, loc, member.id, actor_top(interaction), member.top_role.position
            ),
        )
