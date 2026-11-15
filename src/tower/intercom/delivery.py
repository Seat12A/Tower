"""Delivery of the notifier's cards to Discord channels (loaded by the bot process only).

``build`` returns the two senders injected into the ``Notifier``: ``send`` for ordinary
notifications and ``send_draft`` for fact-check drafts. A channel that is not configured
is ignored; in dry-run mode nothing is sent, the call is only logged.

``ForumDelivery`` is the Discord side of the forum of cases (``forum.ForumSink``): it
creates the missing tags (moderated: only the bot sets them), the pinned "How to review"
post and the forum settings once, then creates, edits and archives posts.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal

import discord

from tower.core.config import Settings
from tower.core.logging import get_logger
from tower.intercom.handlers import Reply
from tower.intercom.handlers import forum as forum_render
from tower.intercom.i18n import t
from tower.intercom.ui import emojis, render

__all__ = ["DryRunForum", "ForumDelivery", "build"]

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


_NO_MENTIONS = discord.AllowedMentions.none()
_ARCHIVE_AFTER: Literal[10080] = 10080  # minutes: a week of silence archives a post


def _tag_label(key: str) -> str:
    return t(forum_render.PUBLIC_LOCALE, f"forum.tag.{key}")


class ForumDelivery:
    """Forum of cases on Discord. The bot needs, on the forum: Manage Channels (tags,
    settings), Manage Threads (moderated tags, archive, pin), Send Messages in Threads."""

    def __init__(self, client: discord.Client, forum_id: int) -> None:
        self._client = client
        self._forum_id = forum_id
        self._tags: dict[str, discord.ForumTag] | None = None

    async def _channel(self, channel_id: int) -> Any:
        return self._client.get_channel(channel_id) or await self._client.fetch_channel(channel_id)

    async def prepare(self) -> None:
        # The notifier starts from setup_hook, before the gateway is ready: wait so the
        # forum and its threads are known (otherwise the guide is never found).
        await self._client.wait_until_ready()
        await self._forum()

    async def _forum(self) -> discord.ForumChannel:
        forum = await self._channel(self._forum_id)
        if not isinstance(forum, discord.ForumChannel):
            raise TypeError("TOWER_DISCORD_FORUM_CASES is not a forum channel")
        if self._tags is None:
            await self._setup(forum)
        return forum

    async def _setup(self, forum: discord.ForumChannel) -> None:
        """Missing tags (by name, never deleted), settings, pinned guide. Once."""
        by_name = {tag.name: tag for tag in forum.available_tags}
        for key in forum_render.TAGS:
            label = _tag_label(key)
            if label not in by_name:
                by_name[label] = await forum.create_tag(name=label, moderated=True)
        self._tags = {key: by_name[_tag_label(key)] for key in forum_render.TAGS}
        if forum.default_sort_order is not discord.ForumOrderType.creation_date:
            await forum.edit(
                default_sort_order=discord.ForumOrderType.creation_date,
                default_layout=discord.ForumLayoutType.list_view,
            )
        await self._ensure_guide(forum)
        log.info("intercom.forum_ready", tags=len(self._tags))

    async def _find_guide(self, forum: discord.ForumChannel) -> discord.Thread | None:
        """The pinned guide, or the bot's own post titled like it, asked to Discord (not
        the cache): active threads first, then the most recent archived ones."""
        title = t(forum_render.PUBLIC_LOCALE, "forum.guide.title")
        me = self._client.user.id if self._client.user else None

        def is_guide(thread: discord.Thread) -> bool:
            return thread.parent_id == forum.id and (
                thread.flags.pinned or (thread.owner_id == me and thread.name == title)
            )

        for thread in await forum.guild.active_threads():
            if is_guide(thread):
                return thread
        async for thread in forum.archived_threads(limit=100):
            if is_guide(thread):
                return thread
        return None

    async def _ensure_guide(self, forum: discord.ForumChannel) -> None:
        """Create the pinned "How to review" post once; pin an existing unpinned one."""
        found = await self._find_guide(forum)
        if found is not None:
            if found.archived or not found.flags.pinned:
                await found.edit(archived=False, pinned=True)
            return
        guide = Reply(
            title=t(forum_render.PUBLIC_LOCALE, "forum.guide.title"),
            icon=emojis.INFO,
            body=t(forum_render.PUBLIC_LOCALE, "forum.guide.body"),
            sections=(t(forum_render.PUBLIC_LOCALE, "forum.guide.tags"),),
            ephemeral=False,
        )
        created = await forum.create_thread(
            name=guide.title, view=_stopped(guide), allowed_mentions=_NO_MENTIONS
        )
        await created.thread.edit(pinned=True)

    def _applied(self, post: forum_render.Post) -> list[discord.ForumTag]:
        tags = self._tags or {}
        return [tags[key] for key in post.tags if key in tags][: forum_render.MAX_TAGS]

    async def create(self, post: forum_render.Post) -> int:
        forum = await self._forum()
        created = await forum.create_thread(
            name=post.name,
            view=_stopped(post.starter),
            applied_tags=self._applied(post),
            auto_archive_duration=_ARCHIVE_AFTER,
            allowed_mentions=_NO_MENTIONS,
        )
        if post.archived:
            await created.thread.edit(archived=True)
        thread_id: int = created.thread.id
        return thread_id

    async def _thread(self, thread_id: int) -> discord.Thread | None:
        try:
            thread = await self._channel(thread_id)
        except (discord.NotFound, discord.Forbidden):
            return None
        return thread if isinstance(thread, discord.Thread) else None

    async def update(self, thread_id: int, post: forum_render.Post) -> bool:
        await self._forum()  # tags ready
        thread = await self._thread(thread_id)
        if thread is None:
            return False
        if thread.archived:
            await thread.edit(archived=False)
        try:
            starter = thread.get_partial_message(thread.id)
            await starter.edit(view=_stopped(post.starter), allowed_mentions=_NO_MENTIONS)
        except discord.NotFound:
            return False  # starter deleted by hand: a new post is created
        await thread.edit(name=post.name, applied_tags=self._applied(post), archived=post.archived)
        return True

    async def reply(self, thread_id: int, reply: Reply) -> None:
        thread = await self._thread(thread_id)
        if thread is None:
            return
        archived = thread.archived
        await render.post(thread, reply)  # sending unarchives the thread
        if archived:
            await thread.edit(archived=True)


def _stopped(reply: Reply) -> discord.ui.LayoutView:
    view = render.layout(reply)
    view.stop()  # clicks are answered by the registered DynamicItems
    return view


class DryRunForum:
    """Forum sink of the dry-run mode: logs, sends nothing (fake thread ids)."""

    def __init__(self) -> None:
        self._next = 0

    async def prepare(self) -> None:
        log.info("intercom.forum_ready", tags=len(forum_render.TAGS))

    async def create(self, post: forum_render.Post) -> int:
        self._next += 1
        log.info("intercom.forum_post", tags=len(post.tags), archived=post.archived)
        return self._next

    async def update(self, thread_id: int, post: forum_render.Post) -> bool:
        log.info("intercom.forum_update", tags=len(post.tags), archived=post.archived)
        return True

    async def reply(self, thread_id: int, reply: Reply) -> None:
        log.info("intercom.forum_reply", text_length=len(reply.text))
