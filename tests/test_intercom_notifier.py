from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any, cast

from tower.core.domain.cases import with_legacy_topics
from tower.intercom.gate_client import GateClient, GateError, GateUnavailable, SseEvent
from tower.intercom.handlers import Reply
from tower.intercom.notifier import TOPIC_CHANNELS, Notifier, format_event

CASE = "11111111-2222-3333-4444-555555555555"


def ev(event_id: str, topic: str, payload: dict[str, Any] | None = None) -> SseEvent:
    return SseEvent(event_id, topic, {"topic": topic, "payload": payload or {}})


def case_event(event_id: str) -> SseEvent:
    return ev(event_id, "case.opened", {"public_id": CASE, "title": "T", "x_post_id": "1"})


class FakeGate:
    """Scripted stream: each attempt is a list of events or exceptions to raise."""

    def __init__(self, scripts: list[list[SseEvent | Exception]], cursor: str | None = None):
        self.scripts = scripts
        self.cursor = cursor
        self.calls: list[tuple[str | None, tuple[str, ...]]] = []
        self.stored: list[str] = []
        self.fail_put = False

    async def get_cursor(self, name: str) -> str | None:
        return self.cursor

    async def put_cursor(self, name: str, cursor: str) -> None:
        if self.fail_put:
            raise GateUnavailable(0, "TWR-BOT-001", "down")
        self.stored.append(cursor)
        self.cursor = cursor

    async def stream_events(self, *, after: str | None, topics: Any) -> AsyncIterator[SseEvent]:
        self.calls.append((after, tuple(topics)))
        if not self.scripts:
            await asyncio.Event().wait()  # idle stream until cancelled
        for item in self.scripts.pop(0):
            if isinstance(item, Exception):
                raise item
            yield item


async def run_until(
    gate: FakeGate,
    send: Callable[[str, Reply], Any],
    done: Callable[[], bool],
    **kwargs: Any,
) -> None:
    notifier = Notifier(
        cast(GateClient, gate),
        send,
        cursor_name="intercom",
        locale="en",
        initial_backoff=0.01,
        **kwargs,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(notifier.run(stop))
    try:
        async with asyncio.timeout(5):
            while not done():
                await asyncio.sleep(0.01)
    finally:
        stop.set()
        await asyncio.wait_for(task, 2)


async def test_first_start_has_no_cursor_and_topics_exclude_content() -> None:
    gate = FakeGate([[case_event("1-1")]])
    sent: list[tuple[str, Reply]] = []

    async def send(channel: str, reply: Reply) -> None:
        sent.append((channel, reply))

    await run_until(gate, send, lambda: bool(sent))
    after, topics = gate.calls[0]
    assert after is None
    assert set(topics) == set(with_legacy_topics(list(TOPIC_CHANNELS)))
    assert "content.ingested" not in topics
    assert sent[0][0] == "cases"
    assert gate.stored == ["1-1"]


async def test_resume_dedup_and_cursor_after_each_event() -> None:
    gate = FakeGate(
        [
            [case_event("1-1"), case_event("1-2"), GateUnavailable(0, "TWR-BOT-001", "x")],
            [case_event("1-2"), case_event("1-3"), GateUnavailable(0, "TWR-BOT-001", "x")],
        ],
        cursor="0-9",
    )
    sent: list[str] = []

    async def send(channel: str, reply: Reply) -> None:
        sent.append(reply.text)

    await run_until(gate, send, lambda: len(gate.calls) >= 3 and len(sent) >= 3)
    assert len(sent) == 3  # the duplicate 1-2 was not delivered again
    assert gate.stored == ["1-1", "1-2", "1-3"]
    assert [c[0] for c in gate.calls[:3]] == ["0-9", "1-2", "1-3"]


async def test_unknown_topic_ignored_and_send_failure_does_not_stop() -> None:
    gate = FakeGate([[ev("1-1", "content.ingested"), case_event("1-2"), case_event("1-3")]])
    attempts: list[str] = []

    async def send(channel: str, reply: Reply) -> None:
        attempts.append(reply.text)
        if len(attempts) == 1:
            raise RuntimeError("discord down")

    await run_until(gate, send, lambda: len(attempts) >= 2)
    assert len(attempts) == 2  # content.ingested never sent; failure on the first continued
    assert gate.stored == ["1-1", "1-2", "1-3"]


async def test_cursor_store_failure_is_not_fatal() -> None:
    gate = FakeGate([[case_event("1-1"), case_event("1-2")]])
    gate.fail_put = True
    sent: list[str] = []

    async def send(channel: str, reply: Reply) -> None:
        sent.append(reply.text)

    await run_until(gate, send, lambda: len(sent) == 2)


async def test_seen_set_is_bounded() -> None:
    gate = FakeGate(
        [
            [case_event("1-1"), case_event("1-2"), case_event("1-3")],
            [GateError(500, "TWR-INT-001", "boom")],
            [case_event("1-1")],  # evicted from the seen set: delivered again
        ]
    )
    sent: list[str] = []

    async def send(channel: str, reply: Reply) -> None:
        sent.append(reply.text)

    await run_until(gate, send, lambda: len(sent) >= 4, seen_capacity=2)
    assert len(sent) == 4


async def test_stop_interrupts_an_idle_stream() -> None:
    gate = FakeGate([])  # the stream blocks forever
    notifier = Notifier(cast(GateClient, gate), _noop, cursor_name="c", locale="en")
    stop = asyncio.Event()
    task = asyncio.create_task(notifier.run(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 1)


async def test_stop_interrupts_backoff() -> None:
    gate = FakeGate([[GateUnavailable(0, "TWR-BOT-001", "x")]])
    notifier = Notifier(
        cast(GateClient, gate), _noop, cursor_name="c", locale="en", initial_backoff=30
    )
    stop = asyncio.Event()
    task = asyncio.create_task(notifier.run(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 1)


async def _noop(channel: str, reply: Reply) -> None:
    return None


def text_of(formatted: tuple[str, Reply] | None) -> str:
    assert formatted is not None
    return formatted[1].text


def test_format_event_cards_include_platform() -> None:
    watch = ev(
        "1-1",
        "watchlist.changed",
        {"action": "added", "platform": "bluesky", "handle": "jdoe", "category": "journalist"},
    )
    out = format_event(watch, "en")
    assert out is not None and out[0] == "admin"
    assert out[1].title == "Watchlist: account added" and not out[1].ephemeral
    assert "bluesky" in out[1].text and "@jdoe" in out[1].text
    fr = format_event(watch, "fr")
    assert fr is not None and "ajouté" in fr[1].title and "bluesky" in fr[1].text

    deactivated = ev(
        "1-2", "watchlist.changed", {"action": "updated", "platform": "x", "active": False}
    )
    assert "deactivated" in text_of(format_event(deactivated, "en"))

    opened = format_event(case_event("1-3"), "en")
    assert opened is not None and opened[0] == "cases" and "`x`" in opened[1].text
    assert opened[1].accent == "primary"
    moved = format_event(
        ev(
            "1-4",
            "case.transitioned",
            {"public_id": CASE, "from": "received", "to": "in_review"},
        ),
        "en",
    )
    assert moved is not None and "`received`" in moved[1].text and "`in review`" in moved[1].text


def test_format_event_incident_and_budget() -> None:
    inc = format_event(
        ev(
            "1-1",
            "incident.opened",
            {"fingerprint": "abcdef0123456789", "code": "TWR-MNT-001", "title": "Task failed"},
        ),
        "en",
    )
    assert inc is not None and inc[0] == "incidents" and inc[1].accent == "error"
    assert "TWR-MNT-001" in inc[1].text and "Task failed" in inc[1].text
    budget_payload = {
        "scope": "provider:x",
        "threshold_pct": 80,
        "spent_usd_micros": 8_000_000,
        "cap_usd_micros": 10_000_000,
    }
    budget = format_event(ev("1-2", "budget.threshold_reached", budget_payload), "en")
    assert budget is not None and budget[0] == "budget" and budget[1].accent == "warning"
    assert "provider:x" in budget[1].text and "$8.00" in budget[1].text and "80%" in budget[1].text
    full = format_event(
        ev("1-5", "budget.threshold_reached", {**budget_payload, "threshold_pct": 100}), "en"
    )
    assert full is not None and full[1].accent == "error"
    assert format_event(ev("1-3", "content.ingested"), "en") is None
    assert format_event(ev("1-4", "ping.done"), "en") is None


def test_every_notification_card_is_public_and_has_an_icon() -> None:
    from tests.intercom_ui import assert_persistent

    events = [
        ev("1", "incident.reopened", {"code": "TWR-X-001"}),
        ev("2", "extension_filters.published", {"version": 3}),
        ev("3", "narrative.surge_detected", {"narrative_id": CASE, "tier": "surge"}),
        ev("4", "narrative.surge_ended", {"narrative_id": CASE}),
    ]
    for event in events:
        out = format_event(event, "fr")
        assert out is not None
        reply = out[1]
        assert not reply.ephemeral and reply.icon and reply.title
        assert_persistent(reply)
