"""``/help``: the commands the member can use, by area."""

from __future__ import annotations

from typing import Any

import discord
from discord import app_commands

from tower.intercom.commands import Context
from tower.intercom.handlers import team as h
from tower.intercom.ui.base import run_handler

DYNAMIC_ITEMS: tuple[type, ...] = ()
SAMPLE_CUSTOM_IDS: tuple[str, ...] = ()


def register(tree: app_commands.CommandTree[Any], ctx: Context) -> None:
    gate = ctx.gate

    @tree.command(name="help", description="List the commands you can use")
    @app_commands.guild_only()
    async def help_(interaction: discord.Interaction) -> None:
        await run_handler(interaction, lambda loc, uid: h.help_card(gate, uid, loc))
