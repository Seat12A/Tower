"""Discord notifications fed by gate's SSE stream.

With a forum configured (``forum``), case events keep one forum post per case up
to date (``tower.intercom.forum``); without one, they are posted as cards in the
cases text channel (fallback). Public messages are always in English.

The notifier resumes from a cursor stored in gate (``intercom_cursor_name``), delivers
at least once and de-duplicates by event id in a bounded in-memory set. Every
notification is a Components V2 card (``Reply``) sent through an injected
``send(channel_key, reply)`` so it is testable without Discord. Public messages use the
default locale and mention nobody, except the configured alert role on wave alerts.
High-volume topics (``content.ingested``) are never subscribed to.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from tower.core.domain import error_codes as ec
from tower.core.domain.cases import LEGACY_TOPICS, current_topic, with_legacy_topics
from tower.core.errors import capture
from tower.core.factcheck.names import (
    DRAFT_READY,
    DRAFT_REVIEWED,
    NARRATIVE_SURGE_DETECTED,
    NARRATIVE_SURGE_ENDED,
)
from tower.core.logging import get_logger
from tower.intercom.forum import CaseForum
from tower.intercom.gate_client import GateClient, GateError, SseEvent
from tower.intercom.handlers import Accent, Reply, card, code, fmt_usd, short
from tower.intercom.handlers.factcheck import SendDraft, post_draft, reconcile_drafts
from tower.intercom.handlers.narratives import format_surge_detected, format_surge_ended
from tower.intercom.i18n import t
from tower.intercom.ui import emojis

__all__ = ["TOPIC_CHANNELS", "Notifier", "Sender", "format_event"]

log = get_logger(__name__)

Sender = Callable[[str, Reply], Awaitable[None]]
"""``(channel key, card)``: posts the card; a channel that is not configured is ignored."""

TOPIC_CHANNELS: dict[str, str] = {
    "incident.opened": "incidents",
    "incident.reopened": "incidents",
    "budget.threshold_reached": "budget",
    "case.opened": "cases",
    "case.transitioned": "cases",
    DRAFT_READY: "cases",
    NARRATIVE_SURGE_DETECTED: "narratives",
    NARRATIVE_SURGE_ENDED: "narratives",
    "watchlist.changed": "admin",
    "extension_filters.published": "admin",
}

FORUM_TOPICS = frozenset({"case.opened", "case.transitioned", DRAFT_READY, DRAFT_REVIEWED})
"""Topics handled by the forum of cases when it is configured."""

_MAX_BACKOFF = 30.0


def _payload(event: SseEvent) -> dict[str, Any]:
    payload = event.data.get("payload")
    return payload if isinstance(payload, dict) else {}


def _account(p: dict[str, Any]) -> str:
    if p.get("handle"):
        return f"@{p['handle']}"
    return str(p.get("external_id") or "?")


def _platform(p: dict[str, Any]) -> str:
    return str(p.get("platform") or ("x" if p.get("x_post_id") else "-"))


def _notification(
    locale: str,
    title_key: str,
    icon: str,
    accent: Accent,
    *,
    body: str = "",
    details: tuple[tuple[str, str], ...] = (),
) -> Reply:
    return card(
        t(locale, title_key), body, icon=icon, accent=accent, details=details, ephemeral=False
    )


def _detail(locale: str, name: str, value: object) -> tuple[str, str]:
    return t(locale, f"notify.label.{name}"), code(value)


def format_event(event: SseEvent, locale: str) -> tuple[str, Reply] | None:
    """``(channel key, card)`` of an event, or ``None`` when it is not notified."""
    channel = TOPIC_CHANNELS.get(event.topic)
    if channel is None or event.topic == DRAFT_READY:  # drafts are rendered by the notifier
        return None
    p = _payload(event)
    topic = event.topic
    reply: Reply
    if topic in ("incident.opened", "incident.reopened"):
        reply = _notification(
            locale,
            f"notify.title.{topic}",
            emojis.ERROR,
            "error",
            body=str(p.get("title", "")),
            details=(
                _detail(locale, "code", p.get("code", "?")),
                _detail(locale, "process", p.get("process", "?")),
                _detail(locale, "fingerprint", short(p.get("fingerprint", "?"), 12)),
            ),
        )
    elif topic == "budget.threshold_reached":
        try:
            reached_cap = int(p.get("threshold_pct", 0)) >= 100
        except (TypeError, ValueError):
            reached_cap = False
        reply = _notification(
            locale,
            "notify.title.budget",
            emojis.WARNING,
            "error" if reached_cap else "warning",
            details=(
                _detail(locale, "scope", p.get("scope", "?")),
                _detail(locale, "threshold", f"{p.get('threshold_pct', '?')}%"),
                _detail(locale, "spent", f"${fmt_usd(p.get('spent_usd_micros', 0))}"),
                _detail(locale, "cap", f"${fmt_usd(p.get('cap_usd_micros', 0))}"),
            ),
        )
    elif topic == "case.opened":
        reply = _notification(
            locale,
            "notify.title.case.opened",
            emojis.FOLDER,
            "primary",
            body=str(p.get("title") or "-"),
            details=(
                _detail(locale, "case", short(p.get("public_id", "?"))),
                _detail(locale, "platform", _platform(p)),
            ),
        )
    elif topic == "case.transitioned":
        reply = _notification(
            locale,
            "notify.title.case.transitioned",
            emojis.FOLDER,
            "primary",
            details=(
                _detail(locale, "case", short(p.get("public_id", "?"))),
                _detail(locale, "platform", _platform(p)),
                _detail(locale, "from", _status(locale, p.get("from"))),
                _detail(locale, "to", _status(locale, p.get("to"))),
            ),
        )
    elif topic == NARRATIVE_SURGE_DETECTED:
        return channel, format_surge_detected(p, locale)
    elif topic == NARRATIVE_SURGE_ENDED:
        return channel, format_surge_ended(p, locale)
    elif topic == "watchlist.changed":
        action = str(p.get("action", "updated"))
        if action == "updated" and p.get("active") is False:
            action = "deactivated"
        reply = _notification(
            locale,
            f"notify.title.watchlist.{action}",
            emojis.EYE,
            "primary",
            details=(
                _detail(locale, "platform", p.get("platform", "?")),
                _detail(locale, "account", _account(p)),
                _detail(locale, "category", p.get("category", "?")),
            ),
        )
    else:  # extension_filters.published
        reply = _notification(
            locale,
            "notify.title.extension_filters.published",
            emojis.FILTER,
            "primary",
            details=(_detail(locale, "version", p.get("version", "?")),),
        )
    return channel, reply


def _status(locale: str, status: object) -> str:
    key = f"status.{status}"
    translated = t(locale, key)
    return str(status) if translated == key else translated


def _wave_key(event: SseEvent) -> tuple[str, str] | None:
    """``(narrative, tier)`` of a surge notification: one message per pair, even when
    gate replays the event (the surge may be published twice: ``surge`` then ``alert``)."""
    if event.topic != NARRATIVE_SURGE_DETECTED:
        return None
    p = _payload(event)
    return str(p.get("narrative_id", "")), str(p.get("tier", ""))


class Notifier:
    def __init__(
        self,
        gate: GateClient,
        send: Sender,
        *,
        cursor_name: str,
        locale: str,
        seen_capacity: int = 2048,
        initial_backoff: float = 1.0,
        send_draft: SendDraft | None = None,
        alert_role_id: int | None = None,
        forum: CaseForum | None = None,
    ) -> None:
        """``alert_role_id``: Discord role mentioned at the top of a wave alert
        (``narrative.surge_detected`` at tier ``alert``); ``None`` = no mention.
        ``forum``: forum of cases; ``None`` = cards in the cases channel."""
        self._gate = gate
        self._send = send
        self._cursor_name = cursor_name
        self._locale = locale
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._waves: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._capacity = seen_capacity
        self._cursor: str | None = None
        self._progressed = False
        self._initial_backoff = initial_backoff
        self._send_draft = send_draft
        self._alert_role_id = alert_role_id
        self._forum = forum
        self._reconciled = send_draft is None and forum is None

    def _mark_seen(self, event_id: str) -> None:
        self._seen[event_id] = None
        self._seen.move_to_end(event_id)
        while len(self._seen) > self._capacity:
            self._seen.popitem(last=False)

    def _wave_already_notified(self, event: SseEvent) -> bool:
        """Records a surge notification; the end of a wave frees its narrative so a later
        wave of the same narrative is notified again."""
        key = _wave_key(event)
        if key is not None:
            if key in self._waves:
                return True
            self._waves[key] = None
            while len(self._waves) > self._capacity:
                self._waves.popitem(last=False)
        elif event.topic == NARRATIVE_SURGE_ENDED:
            narrative = str(_payload(event).get("narrative_id", ""))
            for tier in ("surge", "alert"):
                self._waves.pop((narrative, tier), None)
        return False

    async def _wait(self, stop: asyncio.Event, delay: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), delay)

    async def _notify_draft(self, event: SseEvent) -> None:
        """``draft.ready``: fetch the draft, post it with its buttons, mark it notified.
        A failure is stored; the reconciliation of the next start posts it."""
        if self._send_draft is None:
            return
        draft_id = _payload(event).get("draft_id")
        if not draft_id:
            return
        try:
            view = await self._gate.get_draft(None, str(draft_id))
            if view.get("notified_at") or view.get("status") != "draft":
                return  # already displayed (replayed event) or already decided
            await post_draft(self._gate, self._send_draft, view, self._locale)
        except Exception as exc:
            capture(ec.BOT_DELIVERY_FAILED, exc, topic=event.topic, channel="cases")

    @property
    def topics(self) -> list[str]:
        """Topics read from gate's stream."""
        topics = list(TOPIC_CHANNELS)
        if self._forum is not None:
            topics.append(DRAFT_REVIEWED)
        return with_legacy_topics(topics)

    async def _forum_event(self, event: SseEvent) -> None:
        """A case event, forum mode. A failure is stored and the loop goes on; the
        reconciliation of the next start shows drafts that were never displayed."""
        if self._forum is None:
            return
        p = _payload(event)
        try:
            if event.topic == "case.opened":
                await self._forum.on_case_opened(p)
            elif event.topic == DRAFT_READY:
                await self._forum.on_draft_ready(p)
            elif event.topic == DRAFT_REVIEWED:
                await self._forum.on_draft_reviewed(p)
            else:
                await self._forum.on_case_transitioned(p, event.data.get("actor"))
        except Exception as exc:
            capture(ec.BOT_DELIVERY_FAILED, exc, topic=event.topic, channel="forum")

    async def reconcile(self) -> int:
        """Post the pending drafts never displayed (startup, before reading events)."""
        if self._forum is not None:
            return await reconcile_drafts(self._gate, self._forum.show_draft)
        if self._send_draft is None:
            return 0
        send = self._send_draft

        async def post(view: dict[str, Any]) -> bool:
            return await post_draft(self._gate, send, view, self._locale)

        return await reconcile_drafts(self._gate, post)

    async def _handle(self, event: SseEvent) -> None:
        if event.id in self._seen:
            return
        if event.topic in LEGACY_TOPICS:  # published before the dossier -> case rename
            event = dataclasses.replace(event, topic=current_topic(event.topic))
        formatted = format_event(event, self._locale)
        if self._forum is not None and event.topic in FORUM_TOPICS:
            await self._forum_event(event)
        elif event.topic == DRAFT_READY:
            await self._notify_draft(event)
        elif formatted is not None and not self._wave_already_notified(event):
            channel, reply = formatted
            if (
                self._alert_role_id is not None
                and event.topic == NARRATIVE_SURGE_DETECTED
                and _payload(event).get("tier") == "alert"
            ):
                # The delivery layer lets this one role mention through (and nothing else).
                reply = dataclasses.replace(reply, body=f"<@&{self._alert_role_id}>\n{reply.body}")
            try:
                await self._send(channel, reply)
            except Exception as exc:
                capture(ec.BOT_DELIVERY_FAILED, exc, topic=event.topic, channel=channel)
        self._mark_seen(event.id)
        self._cursor = event.id
        self._progressed = True
        try:
            await self._gate.put_cursor(self._cursor_name, event.id)
        except GateError as exc:
            log.warning("intercom.cursor_store_failed", code=exc.code, status=exc.status)

    async def _consume(self, stop: asyncio.Event) -> None:
        stream = self._gate.stream_events(after=self._cursor, topics=self.topics)
        try:
            async for event in stream:
                if stop.is_set():
                    return
                await self._handle(event)
        finally:
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                await aclose()

    async def run(self, stop: asyncio.Event) -> None:
        backoff = self._initial_backoff
        while not stop.is_set():
            if self._cursor is None:
                try:
                    self._cursor = await self._gate.get_cursor(self._cursor_name)
                except GateError as exc:
                    capture(ec.BOT_GATE_UNREACHABLE, exc, status=exc.status)
                    await self._wait(stop, backoff)
                    backoff = min(backoff * 2, _MAX_BACKOFF)
                    continue
            if not self._reconciled:
                try:
                    await self.reconcile()
                    self._reconciled = True
                except GateError as exc:
                    capture(ec.BOT_GATE_UNREACHABLE, exc, status=exc.status)
                    await self._wait(stop, backoff)
                    backoff = min(backoff * 2, _MAX_BACKOFF)
                    continue
            task = asyncio.ensure_future(self._consume(stop))
            waiter = asyncio.ensure_future(stop.wait())
            try:
                await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                waiter.cancel()
                if not task.done():
                    task.cancel()
            failure: GateError | None = None
            try:
                await task
            except asyncio.CancelledError:
                if stop.is_set():
                    return
                raise
            except GateError as exc:
                failure = exc
            if stop.is_set():
                return
            if self._progressed:
                backoff, self._progressed = self._initial_backoff, False
            if failure is not None:
                capture(ec.BOT_STREAM_INTERRUPTED, failure, status=failure.status, retry_in=backoff)
            await self._wait(stop, backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF)
