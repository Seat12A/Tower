"""Discord notifications fed by gate's SSE stream.

The notifier resumes from a cursor stored in gate (``intercom_cursor_name``), delivers
at least once and de-duplicates by event id in a bounded in-memory set. Delivery goes
through an injected ``send(channel_key, text)`` so it is testable without Discord.
High-volume topics (``content.ingested``) are never subscribed to.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from tower.core.domain import error_codes as ec
from tower.core.errors import capture
from tower.core.logging import get_logger
from tower.intercom.gate_client import GateClient, GateError, SseEvent
from tower.intercom.handlers import fmt_usd, short
from tower.intercom.i18n import t

__all__ = ["TOPIC_CHANNELS", "Notifier", "format_event"]

log = get_logger(__name__)

TOPIC_CHANNELS: dict[str, str] = {
    "incident.opened": "incidents",
    "incident.reopened": "incidents",
    "budget.threshold_reached": "budget",
    "dossier.opened": "dossiers",
    "dossier.transitioned": "dossiers",
    "watchlist.changed": "admin",
    "extension_filters.published": "admin",
}

_MAX_BACKOFF = 30.0
_MAX_TEXT = 1900


def _payload(event: SseEvent) -> dict[str, Any]:
    payload = event.data.get("payload")
    return payload if isinstance(payload, dict) else {}


def _account(p: dict[str, Any]) -> str:
    if p.get("handle"):
        return f"@{p['handle']}"
    return str(p.get("external_id") or "?")


def _platform_suffix(p: dict[str, Any]) -> str:
    platform = p.get("platform") or ("x" if p.get("x_post_id") else None)
    return f" ({platform})" if platform else ""


def format_event(event: SseEvent, locale: str) -> tuple[str, str] | None:
    """``(channel key, text)`` of an event, or ``None`` when it is not notified."""
    channel = TOPIC_CHANNELS.get(event.topic)
    if channel is None:
        return None
    p = _payload(event)
    topic = event.topic
    text: str
    if topic in ("incident.opened", "incident.reopened"):
        text = t(
            locale,
            f"notify.{topic}",
            code=p.get("code", "?"),
            title=p.get("title", ""),
            process=p.get("process", "?"),
            short_fp=short(p.get("fingerprint", "?"), 12),
        )
    elif topic == "budget.threshold_reached":
        text = t(
            locale,
            "notify.budget.threshold",
            scope=p.get("scope", "?"),
            threshold=p.get("threshold_pct", "?"),
            spent=fmt_usd(p.get("spent_usd_micros", 0)),
            cap=fmt_usd(p.get("cap_usd_micros", 0)),
        )
    elif topic == "dossier.opened":
        text = t(
            locale,
            "notify.dossier.opened",
            short_id=short(p.get("public_id", "?")),
            platform=_platform_suffix(p),
            title=p.get("title") or "-",
        )
    elif topic == "dossier.transitioned":
        text = t(
            locale,
            "notify.dossier.transitioned",
            short_id=short(p.get("public_id", "?")),
            platform=_platform_suffix(p),
            from_status=_status(locale, p.get("from")),
            to_status=_status(locale, p.get("to")),
        )
    elif topic == "watchlist.changed":
        action = str(p.get("action", "updated"))
        key = f"notify.watchlist.{action}"
        if action == "updated" and p.get("active") is False:
            key = "notify.watchlist.deactivated"
        text = t(
            locale,
            key,
            platform=p.get("platform", "?"),
            account=_account(p),
            category=p.get("category", "?"),
        )
    else:  # extension_filters.published
        text = t(locale, "notify.extension_filters.published", version=p.get("version", "?"))
    return channel, text[:_MAX_TEXT]


def _status(locale: str, status: object) -> str:
    key = f"status.{status}"
    translated = t(locale, key)
    return str(status) if translated == key else translated


class Notifier:
    def __init__(
        self,
        gate: GateClient,
        send: Callable[[str, str], Awaitable[None]],
        *,
        cursor_name: str,
        locale: str,
        seen_capacity: int = 2048,
        initial_backoff: float = 1.0,
    ) -> None:
        self._gate = gate
        self._send = send
        self._cursor_name = cursor_name
        self._locale = locale
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._capacity = seen_capacity
        self._cursor: str | None = None
        self._progressed = False
        self._initial_backoff = initial_backoff

    def _mark_seen(self, event_id: str) -> None:
        self._seen[event_id] = None
        self._seen.move_to_end(event_id)
        while len(self._seen) > self._capacity:
            self._seen.popitem(last=False)

    async def _wait(self, stop: asyncio.Event, delay: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), delay)

    async def _handle(self, event: SseEvent) -> None:
        if event.id in self._seen:
            return
        formatted = format_event(event, self._locale)
        if formatted is not None:
            channel, text = formatted
            try:
                await self._send(channel, text)
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
        stream = self._gate.stream_events(after=self._cursor, topics=list(TOPIC_CHANNELS))
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
