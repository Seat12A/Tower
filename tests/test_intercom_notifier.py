from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any, cast

from tower.intercom.gate_client import GateClient, GateError, GateUnavailable, SseEvent
from tower.intercom.notifier import TOPIC_CHANNELS, Notifier, format_event

DOSSIER = "11111111-2222-3333-4444-555555555555"


def ev(event_id: str, topic: str, payload: dict[str, Any] | None = None) -> SseEvent:
    return SseEvent(event_id, topic, {"topic": topic, "payload": payload or {}})


def dossier_event(event_id: str) -> SseEvent:
    return ev(event_id, "dossier.opened", {"public_id": DOSSIER, "title": "T", "x_post_id": "1"})


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
    send: Callable[[str, str], Any],
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
    gate = FakeGate([[dossier_event("1-1")]])
    sent: list[tuple[str, str]] = []

    async def send(channel: str, text: str) -> None:
        sent.append((channel, text))

    await run_until(gate, send, lambda: bool(sent))
    after, topics = gate.calls[0]
    assert after is None
    assert set(topics) == set(TOPIC_CHANNELS) and "content.ingested" not in topics
    assert sent[0][0] == "dossiers"
    assert gate.stored == ["1-1"]


async def test_resume_dedup_and_cursor_after_each_event() -> None:
    gate = FakeGate(
        [
            [dossier_event("1-1"), dossier_event("1-2"), GateUnavailable(0, "TWR-BOT-001", "x")],
            [dossier_event("1-2"), dossier_event("1-3"), GateUnavailable(0, "TWR-BOT-001", "x")],
        ],
        cursor="0-9",
    )
    sent: list[str] = []

    async def send(channel: str, text: str) -> None:
        sent.append(text)

    await run_until(gate, send, lambda: len(gate.calls) >= 3 and len(sent) >= 3)
    assert len(sent) == 3  # the duplicate 1-2 was not delivered again
    assert gate.stored == ["1-1", "1-2", "1-3"]
    assert [c[0] for c in gate.calls[:3]] == ["0-9", "1-2", "1-3"]


async def test_unknown_topic_ignored_and_send_failure_does_not_stop() -> None:
    gate = FakeGate([[ev("1-1", "content.ingested"), dossier_event("1-2"), dossier_event("1-3")]])
    attempts: list[str] = []

    async def send(channel: str, text: str) -> None:
        attempts.append(text)
        if len(attempts) == 1:
            raise RuntimeError("discord down")

    await run_until(gate, send, lambda: len(attempts) >= 2)
    assert len(attempts) == 2  # content.ingested never sent; failure on the first continued
    assert gate.stored == ["1-1", "1-2", "1-3"]


async def test_cursor_store_failure_is_not_fatal() -> None:
    gate = FakeGate([[dossier_event("1-1"), dossier_event("1-2")]])
    gate.fail_put = True
    sent: list[str] = []

    async def send(channel: str, text: str) -> None:
        sent.append(text)

    await run_until(gate, send, lambda: len(sent) == 2)


async def test_seen_set_is_bounded() -> None:
    gate = FakeGate(
        [
            [dossier_event("1-1"), dossier_event("1-2"), dossier_event("1-3")],
            [GateError(500, "TWR-INT-001", "boom")],
            [dossier_event("1-1")],  # evicted from the seen set: delivered again
        ]
    )
    sent: list[str] = []

    async def send(channel: str, text: str) -> None:
        sent.append(text)

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


async def _noop(channel: str, text: str) -> None:
    return None


def test_format_event_texts_include_platform() -> None:
    watch = ev(
        "1-1",
        "watchlist.changed",
        {"action": "added", "platform": "bluesky", "handle": "jdoe", "category": "journalist"},
    )
    out = format_event(watch, "en")
    assert out is not None and out[0] == "admin"
    assert "bluesky" in out[1] and "@jdoe" in out[1]
    fr = format_event(watch, "fr")
    assert fr is not None and "ajouté" in fr[1] and "bluesky" in fr[1]

    deactivated = ev(
        "1-2", "watchlist.changed", {"action": "updated", "platform": "x", "active": False}
    )
    out = format_event(deactivated, "en")
    assert out is not None and "deactivated" in out[1] and "x" in out[1]

    opened = format_event(dossier_event("1-3"), "en")
    assert opened is not None and opened[0] == "dossiers" and "(x)" in opened[1]
    moved = format_event(
        ev(
            "1-4",
            "dossier.transitioned",
            {"public_id": DOSSIER, "from": "received", "to": "in_review"},
        ),
        "en",
    )
    assert moved is not None and "received -> in review" in moved[1]


def test_format_event_incident_and_budget() -> None:
    inc = format_event(
        ev(
            "1-1",
            "incident.opened",
            {"fingerprint": "abcdef0123456789", "code": "TWR-MNT-001", "title": "Task failed"},
        ),
        "en",
    )
    assert inc is not None and inc[0] == "incidents" and "TWR-MNT-001" in inc[1]
    budget = format_event(
        ev(
            "1-2",
            "budget.threshold_reached",
            {
                "scope": "provider:x",
                "threshold_pct": 80,
                "spent_usd_micros": 8_000_000,
                "cap_usd_micros": 10_000_000,
            },
        ),
        "en",
    )
    assert budget is not None and budget[0] == "budget"
    assert "provider:x" in budget[1] and "$8.00" in budget[1] and "80%" in budget[1]
    assert format_event(ev("1-3", "content.ingested"), "en") is None
    assert format_event(ev("1-4", "ping.done"), "en") is None
