"""``/usage``: LLM free allocation and spend card with a refresh button (``t12a:use:refresh``)."""

import functools
import re
from typing import Any, Self

import discord
from discord import app_commands, ui

from tower.intercom.commands import Context
from tower.intercom.handlers import usage
from tower.intercom.ui.base import guarded_callback, run_handler

SAMPLE_CUSTOM_IDS = (usage.CID_REFRESH,)


@functools.cache
def _kit(ctx: Context) -> Any:
    gate = ctx.gate

    class RefreshButton(ui.DynamicItem[ui.Button[Any]], template=re.escape(usage.CID_REFRESH)):
        def __init__(self) -> None:
            super().__init__(ui.Button(custom_id=usage.CID_REFRESH))

        @classmethod
        async def from_custom_id(
            cls, interaction: discord.Interaction, item: ui.Item[Any], match: re.Match[str], /
        ) -> Self:
            return cls()

        @guarded_callback
        async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
            await run_handler(
                interaction, lambda loc, uid: usage.show_usage(gate, uid, loc), edit=True
            )

    return RefreshButton


def dynamic_items(ctx: Context) -> list[type]:
    return [_kit(ctx)]


def register(tree: Any, ctx: Context) -> None:
    gate = ctx.gate

    @app_commands.command(name="usage", description="LLM free allocation and spend")
    @app_commands.guild_only()
    async def u_show(interaction: discord.Interaction) -> None:
        await run_handler(interaction, lambda loc, uid: usage.show_usage(gate, uid, loc))

    tree.add_command(u_show)
