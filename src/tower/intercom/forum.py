"""Forum of cases: keeps one Discord forum post per case in sync with gate.

Driven by the notifier (one event at a time, so posts are created one after another:
back-pressure instead of bursts). The Discord side is an injected ``ForumSink`` so the
logic is testable without Discord. The post of a case is found again through gate
(``discord_thread_id`` of the case), never through a local cache.

Event flow:

- ``case.opened``: create the post (short card) unless the case already has one;
- ``draft.ready``: make sure the post exists, replace its card by the full draft card,
  post the thread sections, then mark the draft notified;
- ``draft.reviewed``: one activity line, card and tags refreshed;
- ``case.transitioned``: card and tags refreshed (archived once final), activity
  line for transitions that no review explains (taken, published, manual cases).
"""

from __future__ import annotations

from typing import Any, Protocol

from tower.core.domain import error_codes as ec
from tower.core.errors import capture
from tower.core.logging import get_logger
from tower.intercom.gate_client import GateClient, GateError
from tower.intercom.handlers import Reply
from tower.intercom.handlers import forum as render
from tower.intercom.i18n import t

__all__ = ["CaseForum", "ForumSink"]

log = get_logger(__name__)


class ForumSink(Protocol):
    """Discord side of the forum (``delivery.ForumDelivery``, or a fake in tests)."""

    async def prepare(self) -> None:
        """Create the missing tags, settings and pinned guide (startup, idempotent)."""
        ...

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


class CaseForum:
    def __init__(self, gate: GateClient, sink: ForumSink) -> None:
        self._gate = gate
        self._sink = sink

    async def prepare(self) -> None:
        """Ready the forum at startup: a misconfiguration shows at once in the logs
        (``TWR-BOT-004``) instead of at the first case. Never blocks the bot."""
        try:
            await self._sink.prepare()
        except Exception as exc:
            capture(ec.BOT_BAD_CONFIG, exc, reason="forum_setup_failed")

    async def _create(self, case_id: str, post: render.Post) -> int:
        thread_id = await self._sink.create(post)
        try:
            await self._gate.link_discord_thread(case_id, thread_id)
        except GateError as exc:
            # Created but not linked: a later event may create a second post.
            log.warning("intercom.thread_link_failed", code=exc.code, status=exc.status)
        return thread_id

    async def _show(self, case_id: str, thread_id: int | None, post: render.Post) -> int:
        """Update the post of a case, or create it when it is missing."""
        if thread_id is not None and await self._sink.update(thread_id, post):
            return thread_id
        return await self._create(case_id, post)

    async def on_case_opened(self, payload: dict[str, Any]) -> None:
        if _thread_id(payload.get("discord_thread_id")) is not None:
            return
        await self._create(str(payload["public_id"]), render.case_post(payload))

    async def show_draft(self, view: dict[str, Any]) -> bool:
        """Post a draft (``draft.ready`` and the startup reconciliation): card, tags and
        sections, then mark it notified. ``True`` when shown."""
        case = view.get("case") or {}
        if not case.get("id"):
            return False
        draft_id = str(view["draft_id"])
        thread_id = await self._show(
            str(case["id"]),
            _thread_id(case.get("discord_thread_id")),
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
        case = view.get("case") or {}
        thread_id = _thread_id(case.get("discord_thread_id"))
        if thread_id is None or not case.get("id"):
            return  # never displayed in the forum (posted before it existed)
        thread_id = await self._show(str(case["id"]), thread_id, render.draft_post(view))
        await self._sink.reply(thread_id, render.review_activity(payload))

    async def on_case_transitioned(self, payload: dict[str, Any], actor: object) -> None:
        public_id = str(payload["public_id"])
        case = await self._gate.get_case(None, public_id)
        thread_id = _thread_id(case.get("discord_thread_id"))
        draft_id = case.get("draft_id")
        if draft_id:
            view = await self._gate.get_draft(None, str(draft_id))
            if thread_id is None and not view.get("notified_at"):
                return  # draft.ready will create the post with the full card
            post = render.draft_post(view, case.get("status"))
        else:
            post = render.case_post(case)
        thread_id = await self._show(public_id, thread_id, post)
        target = str(payload.get("to") or case.get("status"))
        if target == "published":
            url = case.get("published_url") or "?"
            await self._sink.reply(thread_id, render.activity_line("published", actor, url=url))
        elif not draft_id:
            # Fact-check decisions already have their line (``draft.reviewed``).
            status = t(render.PUBLIC_LOCALE, f"forum.status.{render.status_of(target)}")
            await self._sink.reply(
                thread_id, render.activity_line("transitioned", actor, status=status)
            )
