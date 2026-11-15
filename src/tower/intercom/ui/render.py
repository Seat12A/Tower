"""``Reply`` (pure card data) -> Components V2 ``LayoutView``, and the send/edit helpers.

Anatomy (``docs/design/DESIGN.md`` §2): breadcrumb, ``### <icon> Title``, body, details,
quoted items, labelled fields, separator + grey footer; the action buttons go **outside**
the container. Components carry the ``custom_id`` of a registered ``DynamicItem``: the
view is stopped right after sending so clicks always reach the dynamic item (also after
a restart) and memory stays flat.
"""

from __future__ import annotations

from typing import Any

import discord
from discord import ui

from tower.intercom.handlers import MAX_ITEMS, MAX_MESSAGE, Button, Field, Reply, Select

COLORS: dict[str, int] = {
    "primary": 0x5865F2,
    "success": 0x57F287,
    "warning": 0xFEE75C,
    "error": 0xED4245,
    "critical": 0x992D22,
    "neutral": 0x99AAB5,
    "developer": 0x9B59B6,
}

_STYLES = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
    "link": discord.ButtonStyle.link,
}


def quote(text: str) -> str:
    return "\n".join(f"> {line}" for line in text.split("\n"))


def _emoji(icon: str) -> discord.PartialEmoji | None:
    return discord.PartialEmoji.from_str(icon) if icon else None


def button(spec: Button) -> ui.Button[Any]:
    if spec.url:
        return ui.Button(
            label=spec.label[:80], url=spec.url, emoji=_emoji(spec.icon), disabled=spec.disabled
        )
    return ui.Button(
        label=spec.label[:80] or None,
        custom_id=spec.custom_id,
        emoji=_emoji(spec.icon),
        style=_STYLES[spec.style],
        disabled=spec.disabled,
    )


def select(spec: Select) -> ui.Item[Any]:
    if spec.kind == "user":
        return ui.UserSelect(
            custom_id=spec.custom_id,
            placeholder=spec.placeholder[:150] or None,
            min_values=spec.min_values,
            max_values=spec.max_values,
            disabled=spec.disabled,
        )
    return ui.Select(
        custom_id=spec.custom_id,
        placeholder=spec.placeholder[:150] or None,
        min_values=spec.min_values,
        max_values=min(spec.max_values, max(len(spec.options), 1)),
        disabled=spec.disabled,
        options=[
            discord.SelectOption(
                label=o.label[:100],
                value=o.value,
                description=o.description[:100] or None,
                emoji=_emoji(o.icon),
                default=o.default,
            )
            for o in spec.options[:25]
        ],
    )


def _field_row(field: Field) -> ui.ActionRow[Any]:
    row: ui.ActionRow[Any] = ui.ActionRow()
    if isinstance(field.control, Select):
        row.add_item(select(field.control))
    else:
        for spec in field.control[:5]:
            row.add_item(button(spec))
    return row


def _clip(text: str, budget: int) -> str:
    return text if len(text) <= budget else text[: max(budget - 1, 0)] + "…"


def container(reply: Reply, required_icon: str = "") -> ui.Container[Any]:
    """The card itself (everything but the action bar)."""
    budget = MAX_MESSAGE
    c: ui.Container[Any] = ui.Container(accent_colour=discord.Colour(COLORS[reply.accent]))

    def text(value: str) -> None:
        nonlocal budget
        value = _clip(value, budget)
        if value:
            budget -= len(value)
            c.add_item(ui.TextDisplay(value))

    if reply.breadcrumb:
        text("-# " + " › ".join(p for p in reply.breadcrumb if p))
    if reply.title:
        text(f"### {reply.icon} {reply.title}" if reply.icon else f"### {reply.title}")
    if reply.body:
        text(reply.body)
    for section in reply.sections:
        c.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))
        text(section)
    if reply.details:
        text("\n".join(f"**{k}** · {v}" for k, v in reply.details))
    for item in reply.items[:MAX_ITEMS]:
        text(quote(item))
    if reply.more:
        text(f"-# {reply.more}")
    if reply.fields:
        c.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))
        for field in reply.fields:
            label = f"**{field.label}**{required_icon if field.required else ''}"
            text(f"{label}\n-# {field.help}" if field.help else label)
            c.add_item(_field_row(field))
    if reply.footer:
        c.add_item(ui.Separator(spacing=discord.SeparatorSpacing.small))
        text(f"-# {reply.footer}")
    return c


def layout(reply: Reply) -> ui.LayoutView:
    from tower.intercom.ui import emojis

    view = ui.LayoutView(timeout=None)
    view.add_item(container(reply, emojis.REQUIRED_FIELDS))
    if reply.buttons:
        row: ui.ActionRow[Any] = ui.ActionRow()
        for spec in reply.buttons[:5]:
            row.add_item(button(spec))
        view.add_item(row)
    if reply.menu is not None:
        menu_row: ui.ActionRow[Any] = ui.ActionRow()
        menu_row.add_item(select(reply.menu))
        view.add_item(menu_row)
    return view


def _sendable(reply: Reply) -> ui.LayoutView:
    view = layout(reply)
    # Clicks are answered by the registered DynamicItems, not by this instance.
    view.stop()
    return view


_NO_MENTIONS = discord.AllowedMentions.none()


async def send(interaction: discord.Interaction, reply: Reply) -> None:
    """Answer an interaction with ``reply`` (initial response or follow-up)."""
    view = _sendable(reply)
    if interaction.response.is_done():
        await interaction.followup.send(
            view=view, ephemeral=reply.ephemeral, allowed_mentions=_NO_MENTIONS
        )
    else:
        await interaction.response.send_message(
            view=view, ephemeral=reply.ephemeral, allowed_mentions=_NO_MENTIONS
        )
    for more in reply.extra:
        await interaction.followup.send(
            view=_sendable(more), ephemeral=reply.ephemeral, allowed_mentions=_NO_MENTIONS
        )


async def edit(interaction: discord.Interaction, reply: Reply) -> None:
    """Replace the message the component belongs to (immediate feedback)."""
    view = _sendable(reply)
    if interaction.response.is_done():
        await interaction.edit_original_response(view=view, allowed_mentions=_NO_MENTIONS)
    else:
        await interaction.response.edit_message(view=view, allowed_mentions=_NO_MENTIONS)


async def post(channel: discord.abc.Messageable, reply: Reply) -> discord.Message:
    """Post ``reply`` (and its ``extra`` cards) in a channel; returns the first message."""
    first = await channel.send(view=_sendable(reply), allowed_mentions=_NO_MENTIONS)
    for more in reply.extra:
        await channel.send(view=_sendable(more), allowed_mentions=_NO_MENTIONS)
    return first
