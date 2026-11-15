"""Base classes and error reporting of every view, modal and dynamic item of the bot.

- ``BaseModal``: every modal (Modals V2: ``ui.Label`` + text inputs, selects, radio
  groups...); errors reach ``report_interaction_error``.
- ``guarded_callback``: wraps a ``DynamicItem`` callback (a dynamic item has no
  ``on_error``: without it an exception would vanish).
- ``run_handler``: the standard flow of a command or a click: defer, call the pure
  handler, render the card. An unexpected exception becomes a critical card with the
  ``TWR-INT-001`` code and is captured.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Any

import discord
from discord import ui

from tower.core.domain import error_codes as ec
from tower.core.errors import capture
from tower.intercom.handlers import Reply, card
from tower.intercom.i18n import locale_of, t
from tower.intercom.ui import emojis, render

HandlerCall = Callable[[str, int], Awaitable[Reply]]
"""``(locale, acting discord id) -> Reply``."""


def interaction_locale(interaction: discord.Interaction) -> str:
    return locale_of(str(interaction.locale))


def bug_card(locale: str) -> Reply:
    return card(
        t(locale, "error.bug.title"),
        t(locale, "error.bug.body"),
        icon=emojis.BUG,
        accent="critical",
        footer=f"`{ec.INT_UNEXPECTED.code}`",
    )


async def report_interaction_error(
    interaction: discord.Interaction, error: BaseException, where: str
) -> None:
    capture(ec.INT_UNEXPECTED, error, where=where)
    reply = bug_card(interaction_locale(interaction))
    try:
        await render.send(interaction, reply)
    except discord.HTTPException:
        pass


def guarded_callback[T](
    callback: Callable[[T, discord.Interaction], Awaitable[None]],
) -> Callable[[T, discord.Interaction], Awaitable[None]]:
    @functools.wraps(callback)
    async def wrapper(self: T, interaction: discord.Interaction) -> None:
        try:
            await callback(self, interaction)
        except Exception as exc:
            await report_interaction_error(interaction, exc, type(self).__name__)

    return wrapper


class BaseModal(ui.Modal):
    async def on_error(  # type: ignore[override]
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        await report_interaction_error(interaction, error, f"Modal:{type(self).__name__}")


async def run_handler(
    interaction: discord.Interaction,
    call: HandlerCall,
    *,
    edit: bool = False,
    ephemeral: bool = True,
) -> Reply | None:
    """Defer (gate may exceed Discord's 3 s), run ``call``, then send or edit the card.

    ``edit=True`` replaces the message of the clicked component (panels)."""
    locale = interaction_locale(interaction)
    if not interaction.response.is_done():
        if edit:
            await interaction.response.defer()
        else:
            await interaction.response.defer(ephemeral=ephemeral, thinking=True)
    try:
        reply = await call(locale, interaction.user.id)
    except Exception as exc:
        capture(ec.INT_UNEXPECTED, exc, where=getattr(interaction.command, "name", "component"))
        reply = bug_card(locale)
        await render.send(interaction, reply)
        return None
    if edit and reply.accent not in ("error", "warning", "critical"):
        await render.edit(interaction, reply)
    else:
        # Errors on a panel go to a private follow-up: the panel itself stays as it was.
        await render.send(interaction, reply)
    return reply


def emoji(icon: str) -> Any:
    return discord.PartialEmoji.from_str(icon) if icon else None
