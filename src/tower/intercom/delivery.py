"""Delivery of the notifier's cards to Discord channels (loaded by the bot process only).

``build`` returns the two senders injected into the ``Notifier``: ``send`` for ordinary
notifications and ``send_draft`` for fact-check drafts. A channel that is not configured
is ignored; in dry-run mode nothing is sent, the call is only logged.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import discord

from tower.core.config import Settings
from tower.core.logging import get_logger
from tower.intercom.handlers import Reply
from tower.intercom.ui import render

__all__ = ["build"]

log = get_logger(__name__)


async def _channel(
    client: discord.Client, channel_ids: dict[str, int | None], key: str
) -> discord.abc.Messageable | None:
    channel_id = channel_ids.get(key)
    if not channel_id:
        return None
    channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
    if not isinstance(channel, discord.abc.Messageable):
        raise TypeError(f"channel {key} is not messageable")
    return channel


def build(
    client: discord.Client,
    settings: Settings,
    channel_ids: dict[str, int | None],
    *,
    dry_run: bool,
) -> tuple[Callable[[str, Reply], Awaitable[None]], Callable[[str, Reply, str], Awaitable[bool]]]:
    """``(send, send_draft)`` for the notifier."""
    alert_role = settings.discord_alert_role_id
    alert_mention = f"<@&{alert_role}>" if alert_role else ""
    alert_mentions: Any = (
        discord.AllowedMentions(everyone=False, users=False, roles=[discord.Object(id=alert_role)])
        if alert_role
        else None
    )

    async def send(channel_key: str, reply: Reply) -> None:
        channel = await _channel(client, channel_ids, channel_key)
        if channel is None:
            return
        if alert_mention and alert_mention in reply.body:
            # Only the configured alert role may ping, and only on the card carrying it.
            view = render.layout(reply)
            view.stop()
            await channel.send(view=view, allowed_mentions=alert_mentions)
            for more in reply.extra:
                await render.post(channel, more)
            return
        await render.post(channel, reply)

    async def send_draft(channel_key: str, reply: Reply, draft_id: str) -> bool:
        """Post the draft cards; the persistent buttons are on the last one."""
        channel = await _channel(client, channel_ids, channel_key)
        if channel is None:
            return False
        await render.post(channel, reply)
        return True

    async def send_dry_run(channel_key: str, reply: Reply) -> None:
        log.info("intercom.notification", channel=channel_key, text_length=len(reply.text))

    async def send_draft_dry_run(channel_key: str, reply: Reply, draft_id: str) -> bool:
        log.info("intercom.draft_notification", channel=channel_key, cards=1 + len(reply.extra))
        return False

    if dry_run:
        return send_dry_run, send_draft_dry_run
    return send, send_draft
