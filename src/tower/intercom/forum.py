"""Forum of dossiers: keeps one Discord forum post per dossier in sync with gate.

Driven by the notifier (one event at a time, so posts are created one after another:
back-pressure instead of bursts). The Discord side is an injected ``ForumSink`` so the
logic is testable without Discord. The post of a dossier is found again through gate
(``discord_thread_id`` of the dossier), never through a local cache.

Event flow:

- ``dossier.opened``: create the post (short card) unless the dossier already has one;
- ``draft.ready``: make sure the post exists, replace its card by the full draft card,
  post the thread sections, then mark the draft notified;
- ``draft.reviewed``: one activity line, card and tags refreshed;
- ``dossier.transitioned``: card and tags refreshed (archived once final), activity
  line for transitions that no review explains (taken, published, manual dossiers).
"""

from __future__ import annotations

from typing import Any, Protocol

from tower.core.logging import get_logger
from tower.intercom.gate_client import GateClient, GateError
from tower.intercom.handlers import Reply
from tower.intercom.handlers import forum as render
from tower.intercom.i18n import t

__all__ = ["DossierForum", "ForumSink"]

log = get_logger(__name__)


class ForumSink(Protocol):
    """Discord side of the forum (``delivery.ForumDelivery``, or a fake in tests)."""

    async def create(self, post: render.Post) -> int:
        """Create the post; returns its thread id."""
        ...

    async def update(self, thread_id: int, post: render.Post) -> bool:
        """Edit the starter, tags and archived state; ``False`` when the thread is gone."""
        ...

    async def reply(self, thread_id: int, reply: Reply) -> None:
        """Post a card in the thread (unarchives it first when needed)."""
        ...


def _thread_id(value: object) -> int | None:
    try:
        return int(str(value)) if value else None
    except ValueError:
        return None


class DossierForum:
    def __init__(self, gate: GateClient, sink: ForumSink) -> None:
        self._gate = gate
        self._sink = sink

    async def _create(self, dossier_id: str, post: render.Post) -> int:
        thread_id = await self._sink.create(post)
        try:
            await self._gate.link_discord_thread(dossier_id, thread_id)
        except GateError as exc:
            # Created but not linked: a later event may create a second post.
            log.warning("intercom.thread_link_failed", code=exc.code, status=exc.status)
        return thread_id

    async def _show(self, dossier_id: str, thread_id: int | None, post: render.Post) -> int:
        """Update the post of a dossier, or create it when it is missing."""
        if thread_id is not None and await self._sink.update(thread_id, post):
            return thread_id
        return await self._create(dossier_id, post)

    async def on_dossier_opened(self, payload: dict[str, Any]) -> None:
        if _thread_id(payload.get("discord_thread_id")) is not None:
            return
        await self._create(str(payload["public_id"]), render.dossier_post(payload))

    async def show_draft(self, view: dict[str, Any]) -> bool:
        """Post a draft (``draft.ready`` and the startup reconciliation): card, tags and
        sections, then mark it notified. ``True`` when shown."""
        dossier = view.get("dossier") or {}
        if not dossier.get("id"):
            return False
        draft_id = str(view["draft_id"])
        thread_id = await self._show(
            str(dossier["id"]),
            _thread_id(dossier.get("discord_thread_id")),
            render.draft_post(view),
        )
        for card in render.draft_sections(view):
            await self._sink.reply(thread_id, card)
        try:
            await self._gate.mark_draft_notified(None, draft_id)
        except GateError as exc:
            log.warning("intercom.draft_mark_failed", code=exc.code, status=exc.status)
        return True

    async def on_draft_ready(self, payload: dict[str, Any]) -> None:
        draft_id = payload.get("draft_id")
        if not draft_id:
            return
        view = await self._gate.get_draft(None, str(draft_id))
        if view.get("notified_at") or view.get("status") != "draft":
            return  # already displayed (replayed event) or already decided
        await self.show_draft(view)

    async def on_draft_reviewed(self, payload: dict[str, Any]) -> None:
        draft_id = payload.get("draft_id")
        if not draft_id:
            return
        view = await self._gate.get_draft(None, str(draft_id))
        dossier = view.get("dossier") or {}
        thread_id = _thread_id(dossier.get("discord_thread_id"))
        if thread_id is None or not dossier.get("id"):
            return  # never displayed in the forum (posted before it existed)
        thread_id = await self._show(str(dossier["id"]), thread_id, render.draft_post(view))
        await self._sink.reply(thread_id, render.review_activity(payload))

    async def on_dossier_transitioned(self, payload: dict[str, Any], actor: object) -> None:
        public_id = str(payload["public_id"])
        dossier = await self._gate.get_dossier(None, public_id)
        thread_id = _thread_id(dossier.get("discord_thread_id"))
        draft_id = dossier.get("draft_id")
        if draft_id:
            view = await self._gate.get_draft(None, str(draft_id))
            if thread_id is None and not view.get("notified_at"):
                return  # draft.ready will create the post with the full card
            post = render.draft_post(view, dossier.get("status"))
        else:
            post = render.dossier_post(dossier)
        thread_id = await self._show(public_id, thread_id, post)
        target = str(payload.get("to") or dossier.get("status"))
        if target == "published":
            url = dossier.get("published_url") or "?"
            await self._sink.reply(thread_id, render.activity_line("published", actor, url=url))
        elif not draft_id:
            # Fact-check decisions already have their line (``draft.reviewed``).
            status = t(render.PUBLIC_LOCALE, f"forum.status.{render.status_of(target)}")
            await self._sink.reply(
                thread_id, render.activity_line("transitioned", actor, status=status)
            )
