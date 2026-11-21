from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any, cast

import httpx
import pytest
import uvicorn

from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain import error_codes as ec
from tower.core.domain.security import Permission
from tower.core.errors import get_reporter
from tower.core.events.outbox import publish
from tower.intercom.gate_client import GateClient, GateError, GateUnavailable, SseEvent
from tower.intercom.handlers import cases, incidents, usage, watchlist

from .conftest import TokenFactory
from .intercom_ui import assert_persistent

REVIEWER = 1001
EDITOR = 1002
ADMIN = 1003
POST = "1850000000000000042"
AddStaff = Callable[..., Coroutine[Any, Any, None]]


@pytest.fixture
async def bot(
    client: httpx.AsyncClient, make_token: TokenFactory, add_staff: AddStaff
) -> GateClient:
    token = await make_token("intercom")
    await add_staff(REVIEWER, ["reviewer"])
    await add_staff(EDITOR, ["editor"])
    await add_staff(ADMIN, ["admin"])
    return GateClient(base_url="http://gate", token=token, http=client)


async def open_case(gate: GateClient, acting: int = EDITOR) -> str:
    r = await gate._http.post(
        "/v1/cases",
        json={"kind": "fact_check", "x_post_id": POST},
        headers=gate._headers(acting),
    )
    assert r.status_code == 201, r.text
    return str(r.json()["public_id"])


# --- watchlist ---------------------------------------------------------------------


async def test_watchlist_add_list_edit_deactivate(bot: GateClient) -> None:
    r = await watchlist.add(bot, EDITOR, "en", "x", "@Some_Account", "journalist", 5, "note")
    assert r.ephemeral and r.accent == "success" and "@Some_Account" in r.text
    assert_persistent(r)

    listed = await watchlist.list_accounts(bot, REVIEWER, "en", None)
    assert "@Some_Account" in listed.text and "resolution pending" in listed.text
    assert "journalist" in listed.text and "priority 5" in listed.text
    assert_persistent(listed)
    row = (await bot.list_watched(EDITOR))[0]
    assert row["public_id"][:8] in listed.text

    r = await watchlist.edit(bot, EDITOR, "en", row["public_id"][:8], priority=42)
    assert r.title == "Account updated"
    assert (await bot.list_watched(EDITOR))[0]["priority"] == 42

    r = await watchlist.edit(bot, EDITOR, "en", row["public_id"][:8])
    assert "Nothing to change" in r.text

    shown = await watchlist.show_account(bot, EDITOR, "en", row["public_id"][:8])
    assert "@Some_Account" in shown.text and any(
        b.custom_id.startswith("t12a:wl:off:") for b in shown.buttons
    )
    assert_persistent(shown)
    confirm = await watchlist.confirm_deactivate(bot, EDITOR, "en", row["public_id"][:8])
    assert confirm.accent == "warning" and "t12a:wl:offyes:" in confirm.buttons[1].custom_id
    assert_persistent(confirm)
    r = await watchlist.deactivate(bot, EDITOR, "en", row["public_id"])
    assert r.title == "Account deactivated"
    assert await bot.list_watched(EDITOR) == []
    assert "No watched account." in (await watchlist.list_accounts(bot, EDITOR, "en", None)).text


async def test_watchlist_errors(bot: GateClient) -> None:
    r = await watchlist.add(bot, REVIEWER, "en", "x", "someone", "journalist")
    assert r.ephemeral and "TWR-AUTH-005" in r.text  # reviewers cannot write
    await watchlist.add(bot, EDITOR, "en", "x", "someone", "journalist")
    r = await watchlist.add(bot, EDITOR, "en", "x", "someone", "journalist")
    assert "TWR-ING-009" in r.text
    r = await watchlist.edit(bot, EDITOR, "en", "00000000", priority=1)
    assert "TWR-ING-008" in r.text
    r = await watchlist.edit(bot, EDITOR, "fr", "abc", priority=1)
    assert "TWR-GEN-001" in r.text


async def test_french_and_english_replies(bot: GateClient) -> None:
    en = await watchlist.add(bot, EDITOR, "en", "x", "alpha", "journalist")
    fr = await watchlist.add(bot, EDITOR, "fr", "x", "beta", "journalist")
    assert "Account added" in en.text and "Compte ajouté" in fr.text and "résout" in fr.text
    denied = await watchlist.add(bot, REVIEWER, "fr", "x", "gamma", "journalist")
    assert "permission" in denied.text and "TWR-AUTH-005" in denied.text


# --- cases ----------------------------------------------------------------------


async def test_case_happy_path(bot: GateClient) -> None:
    pid = await open_case(bot)
    shown = await cases.show(bot, REVIEWER, "en", pid)
    assert pid[:8] in shown.text and "received" in shown.text and POST in shown.text
    assert_persistent(shown)
    assert [b.disabled for b in shown.buttons[:4]] == [False, True, False, True]

    r = await cases.take(bot, REVIEWER, "en", pid)
    assert "taken into review" in r.text and "in review" in r.text
    r = await cases.validate(bot, EDITOR, "en", pid)
    assert "validated" in r.text and r.accent == "primary"
    r = await cases.publish(bot, EDITOR, "fr", pid, f"https://x.com/seat12a/status/{POST}")
    assert "publiez le post vous-même" in r.text and r.accent == "success"
    d = await bot.get_case(EDITOR, pid)
    assert d["status"] == "published" and d["published_url"].endswith(POST)


async def test_case_commands_accept_the_short_id(bot: GateClient) -> None:
    pid = await open_case(bot)
    short_id = pid[:8].upper()
    shown = await cases.show(bot, REVIEWER, "en", short_id)
    assert pid[:8] in shown.text and "received" in shown.text
    r = await cases.take(bot, REVIEWER, "en", f" {short_id} ")
    assert "taken into review" in r.text
    await cases.validate(bot, EDITOR, "en", pid[:6])
    r = await cases.publish(bot, EDITOR, "en", pid[:8], f"https://x.com/seat12a/status/{POST}")
    assert r.accent == "success"
    assert (await bot.get_case(EDITOR, pid))["status"] == "published"
    missing = await cases.show(bot, EDITOR, "en", "ffffffff")
    assert "TWR-GEN-002" in missing.text or "TWR-DOS-001" in missing.text


async def test_case_reject_and_errors(bot: GateClient) -> None:
    pid = await open_case(bot)
    await cases.take(bot, EDITOR, "en", pid)
    r = await cases.reject(bot, EDITOR, "en", pid, "not checkable")
    assert "rejected" in r.text
    assert (await bot.get_case(EDITOR, pid))["status"] == "rejected"
    r = await cases.take(bot, EDITOR, "en", pid)
    assert "TWR-DOS-002" in r.text and r.ephemeral

    pid2 = await open_case(bot)
    await cases.take(bot, REVIEWER, "en", pid2)
    forbidden = await cases.validate(bot, REVIEWER, "en", pid2)
    assert "TWR-AUTH-005" in forbidden.text and forbidden.ephemeral
    r = await cases.publish(bot, EDITOR, "en", pid2, "https://x.com/a/status/1")
    assert "TWR-DOS-002" in r.text
    r = await cases.show(bot, EDITOR, "en", "00000000-0000-0000-0000-000000000000")
    assert "TWR-DOS-001" in r.text
    await cases.validate(bot, EDITOR, "en", pid2)
    r = await cases.publish(bot, EDITOR, "en", pid2, "not a link")
    assert "TWR-DOS-003" in r.text


# --- incidents ---------------------------------------------------------------------


async def test_incidents_list_and_resolve(bot: GateClient) -> None:
    assert "No open incident." in (await incidents.list_incidents(bot, ADMIN, "en")).text
    fp = get_reporter().capture(ec.MNT_TASK_FAILED, RuntimeError("x"))
    await get_reporter().flush()
    listed = await incidents.list_incidents(bot, ADMIN, "en")
    assert ec.MNT_TASK_FAILED.code in listed.text and fp[:12] in listed.text and "x1" in listed.text
    assert_persistent(listed)
    assert listed.fields[0].control.options[0].value == fp  # type: ignore[union-attr]
    r = await incidents.resolve(bot, ADMIN, "fr", fp[:8], "fixed")
    assert "résolu" in r.text and fp[:12] in r.text
    assert_persistent(r)
    assert "Aucun incident ouvert." in (await incidents.list_incidents(bot, ADMIN, "fr")).text
    forbidden = await incidents.list_incidents(bot, EDITOR, "en")
    assert "TWR-AUTH-005" in forbidden.text


async def test_incident_show_filters_and_resolved(bot: GateClient) -> None:
    exc = RuntimeError("X API 403 on tweets.get")
    fp = get_reporter().capture(ec.FCK_PLATFORM_UNAVAILABLE, exc, platform="x", attempt=3)
    await get_reporter().flush()
    r = await incidents.show(bot, ADMIN, "en", fp[:8])
    assert "TWR-FCK-019" in r.title and r.accent == "warning"
    assert "X API 403 on tweets.get" in r.body and "platform = x" in r.body
    assert "attempt = 3" in r.body and "RuntimeError" in r.text and "Retryable: yes" in r.text
    assert "Status: open" in r.text and r.footer == f"`{fp}`"
    assert [b.custom_id for b in r.buttons] == [f"t12a:inc:res:{fp}", "t12a:inc:list"]
    assert_persistent(r)
    filtered = await incidents.list_incidents(bot, ADMIN, "en", code_filter="twr-fck-019")
    assert fp[:12] in filtered.text and "TWR-FCK-019" in filtered.title
    other = await incidents.list_incidents(bot, ADMIN, "en", code_filter="TWR-MNT-001")
    assert "No incident matches this filter." in other.text
    bad = await incidents.list_incidents(bot, ADMIN, "en", code_filter="nope")
    assert "Invalid code" in bad.text and bad.accent == "warning"
    await incidents.resolve(bot, ADMIN, "en", fp, "token rotated")
    assert fp[:12] not in (await incidents.list_incidents(bot, ADMIN, "en")).text
    every = await incidents.list_incidents(bot, ADMIN, "fr", include_resolved=True)
    assert fp[:12] in every.text and "résolus compris" in every.title
    assert [f.control.custom_id for f in every.fields] == ["t12a:inc:show"]  # type: ignore[union-attr]
    done = await incidents.show(bot, ADMIN, "fr", fp[:6])
    assert "résolu le" in done.text and "token rotated" in done.text
    assert [b.custom_id for b in done.buttons] == ["t12a:inc:list"]
    missing = await incidents.show(bot, ADMIN, "en", "ffffffffffffffff")
    assert "TWR-GEN-" in missing.text
    assert "TWR-AUTH-005" in (await incidents.show(bot, EDITOR, "en", fp)).text


# --- usage -------------------------------------------------------------------------


async def test_usage(bot: GateClient, db: Database) -> None:
    r = await usage.show_usage(bot, EDITOR, "en")
    assert r.ephemeral and "Free allocation" in r.text and "Spend this period" in r.text
    denied = await usage.show_usage(bot, REVIEWER, "en")
    assert "TWR-AUTH-005" in denied.text


class FakeUsageGate:
    async def get_usage(self, acting: int) -> dict[str, Any]:
        return {
            "day": "2026-11-11",
            "period_start": "2026-11-01",
            "period_end": "2026-12-01",
            "free": [
                {
                    "provider": "openai",
                    "free_group": "big",
                    "used_tokens": 250,
                    "allowance_tokens": 1000,
                },
                {
                    "provider": "openai",
                    "free_group": "small",
                    "used_tokens": 0,
                    "allowance_tokens": 0,
                },
            ],
            "scopes": [
                {
                    "scope": "global",
                    "spent_usd_micros": 1_234_567,
                    "reserved_usd_micros": 0,
                    "cap_usd_micros": 50_000_000,
                },
                {
                    "scope": "provider:x",
                    "spent_usd_micros": 5_000,
                    "reserved_usd_micros": 0,
                    "cap_usd_micros": 10_000_000,
                },
            ],
        }


async def test_usage_formatting_with_fake_gate() -> None:
    r = await usage.show_usage(cast(GateClient, FakeUsageGate()), 1, "en")
    assert "`openai/big` · 25% (250 / 1000 tokens)" in r.text
    assert "`openai/small` · no allocation" in r.text
    assert "`global` · $1.23 / $50.00" in r.text
    assert "`provider:x` · $0.01 / $10.00" in r.text
    assert len(r.items) == 2
    assert_persistent(r)
    fr = await usage.show_usage(cast(GateClient, FakeUsageGate()), 1, "fr")
    assert "25 %" in fr.text and "1.23 $" in fr.text


async def test_unavailable_reply_with_fake_gate() -> None:
    class Down:
        async def get_usage(self, acting: int) -> dict[str, Any]:
            raise GateUnavailable(0, "TWR-BOT-001", "down")

        async def list_incidents(self, acting: int, **kwargs: Any) -> list[dict[str, Any]]:
            raise GateError(418, "TWR-XXX-999", "teapot")

    r = await usage.show_usage(cast(GateClient, Down()), 1, "en")
    assert r.ephemeral and r.accent == "error" and "TWR-BOT-001" in r.text
    unknown = await incidents.list_incidents(cast(GateClient, Down()), 1, "en")
    assert "teapot" in unknown.text and "TWR-XXX-999" in unknown.footer


# --- gate client -------------------------------------------------------------------


def mock_gate(handler: Callable[[httpx.Request], httpx.Response]) -> GateClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://gate")
    return GateClient("http://gate", "secret-token", http=http)


async def test_client_headers_and_error_mapping() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/llm/usage":
            body = {"error": {"code": "TWR-AUTH-005", "message": "nope", "details": {"a": 1}}}
            return httpx.Response(403, json=body)
        if request.url.path == "/v1/errors":
            return httpx.Response(503)
        if request.url.path == "/v1/cases/x":
            return httpx.Response(502, text="<html>bad gateway</html>")
        return httpx.Response(200, json={"cursor": "5-5", "name": "n"})

    gate = mock_gate(handler)
    with pytest.raises(GateError) as err:
        await gate.get_usage(42)
    assert not isinstance(err.value, GateUnavailable)
    assert (err.value.status, err.value.code, err.value.message) == (403, "TWR-AUTH-005", "nope")
    assert err.value.details == {"a": 1}
    assert seen[0].headers["authorization"] == "Bearer secret-token"
    assert seen[0].headers["x-acting-discord-id"] == "42"
    with pytest.raises(GateUnavailable) as down:
        await gate.list_incidents(42)
    assert down.value.code == "TWR-BOT-001" and down.value.status == 503
    with pytest.raises(GateUnavailable):
        await gate.get_case(42, "x")
    assert await gate.get_cursor("n") == "5-5"
    assert "x-acting-discord-id" not in seen[-1].headers  # the service itself
    assert "secret-token" not in repr(gate)


async def test_client_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    gate = mock_gate(handler)
    with pytest.raises(GateUnavailable) as err:
        await gate.get_usage(1)
    assert err.value.status == 0 and "secret-token" not in str(err.value)


async def test_client_request_bodies() -> None:
    bodies: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        bodies[f"{request.method} {request.url.path}"] = (
            request.content.decode(),
            str(request.url.query, "ascii"),
        )
        return httpx.Response(200, json={})

    gate = mock_gate(handler)
    await gate.transition_case(1, "abc", to="rejected", version=3, note="why")
    await gate.publish_case(1, "abc", url="https://x.com/a/status/1", version=4)
    await gate.list_watched(1, active=None, platform="x", limit=7)
    await gate.put_cursor("intercom", "1-2")
    assert bodies["POST /v1/cases/abc/transitions"][0] == (
        '{"to":"rejected","expected_version":3,"reason":"why"}'
    )
    assert '"expected_version":4' in bodies["POST /v1/cases/abc/publish"][0]
    assert bodies["GET /v1/watched-accounts"][1] == "limit=7&platform=x"
    assert bodies["PUT /v1/events/cursors/intercom"][0] == '{"cursor":"1-2"}'


async def test_client_sse_parsing() -> None:
    body = (
        "retry: 3000\n\n"
        ": keepalive\n\n"
        'id: 1-1\nevent: case.opened\ndata: {"topic": "case.opened", "payload": {"a": 1}}\n\n'
        ": another comment\n"
        "id: 1-2\nevent: incident.opened\n"
        'data: {"topic": "incident.opened",\ndata: "payload": {}}\n\n'
        "data: {not json}\n\n"
        'event: orphan\ndata: {"topic": "x"}\n\n'
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    gate = mock_gate(handler)
    got: list[SseEvent] = []
    with pytest.raises(GateUnavailable):  # EOF raises
        async for event in gate.stream_events(after="0-1", topics=["a.b", "c.d"]):
            got.append(event)
    assert [(e.id, e.topic) for e in got] == [("1-1", "case.opened"), ("1-2", "incident.opened")]
    assert got[0].data["payload"] == {"a": 1}
    query = requests[0].url.params
    assert query.get_list("topics") == ["a.b", "c.d"] and query["after"] == "0-1"
    assert requests[0].headers["authorization"] == "Bearer secret-token"

    with pytest.raises(GateUnavailable):
        async for _ in gate.stream_events(after=None, topics=["a"]):
            pass
    assert "after" not in requests[1].url.params


async def test_client_sse_error_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"error": {"code": "TWR-AUTH-005", "message": "m", "details": {}}}
        )

    with pytest.raises(GateError) as err:
        async for _ in mock_gate(handler).stream_events(after=None, topics=[]):
            pass
    assert err.value.code == "TWR-AUTH-005"


async def test_client_cursors_against_real_gate(bot: GateClient) -> None:
    assert await bot.get_cursor("intercom") is None
    await bot.put_cursor("intercom", "12-34")
    assert await bot.get_cursor("intercom") == "12-34"
    with pytest.raises(GateError) as err:
        await bot.put_cursor("Bad Name!", "1-1")
    assert err.value.code == "TWR-EVT-003"


@pytest.fixture
async def live_url(settings: Settings, db: Database) -> AsyncIterator[str]:
    from tower.gate.app import create_app

    config = uvicorn.Config(
        create_app(settings), host="127.0.0.1", port=0, log_config=None, lifespan="on"
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    await task


async def test_client_streams_from_real_gate(
    live_url: str, make_token: TokenFactory, db: Database
) -> None:
    token = await make_token("intercom", [Permission.EVENTS_READ])
    gate = GateClient(live_url, token)
    try:
        async with db.transaction() as s:
            await publish(s, "case.opened", {"public_id": "p1"})
            await publish(s, "ping.done", {})
        got: list[SseEvent] = []
        async with asyncio.timeout(5):
            stream = gate.stream_events(after="0", topics=["case.opened"])
            async for event in stream:
                got.append(event)
                break
            await cast(Any, stream).aclose()
        assert got[0].topic == "case.opened" and got[0].data["payload"] == {"public_id": "p1"}
    finally:
        await gate.aclose()


# --- isolation ---------------------------------------------------------------------


def _loaded(code: str) -> str:
    out = subprocess.run(  # noqa: S603 - fixed interpreter, constant code
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    )
    return out.stdout.strip()


def test_discord_is_not_loaded_outside_the_bot() -> None:
    probe = "import sys; print('discord' in sys.modules, 'httpx' in sys.modules)"
    assert _loaded("import tower.gate.app;" + probe).split()[0] == "False"
    hangar = (
        "import tower.hangar.main;"
        "from tower.core.services.registry import load_services;"
        "load_services(['ping', 'ingest']);" + probe
    )
    assert _loaded(hangar).split()[0] == "False"
    bot = (
        "import tower.intercom.notifier, tower.intercom.handlers.watchlist,"
        "tower.intercom.gate_client;" + probe
    )
    assert _loaded(bot) == "False False"


# --- ops cards against a fake gate (no database) ----------------------------------------


def _row(i: int, platform: str = "x") -> dict[str, Any]:
    return {
        "public_id": f"{i:08d}-0000-0000-0000-000000000000",
        "platform": platform,
        "handle": f"user{i}",
        "external_id": None if i == 0 else str(i),
        "category": "journalist",
        "priority": i,
        "active": True,
        "version": 1,
    }


async def test_watchlist_paging_and_filter_with_fake_gate() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        platform = request.url.params.get("platform")
        return httpx.Response(200, json=[_row(i, platform or "x") for i in range(11)])

    gate = mock_gate(handler)
    first = await watchlist.list_accounts(gate, 1, "en", None, 0)
    assert len(first.items) == watchlist.PAGE_SIZE and first.footer == "Page 1 / 2"
    assert first.buttons[0].disabled and not first.buttons[1].disabled
    assert first.buttons[1].custom_id == "t12a:wl:page:all:1"
    assert "resolution pending" in first.items[0]
    assert_persistent(first)
    second = await watchlist.list_accounts(gate, 1, "fr", "bluesky", 5)  # page is clamped
    assert len(second.items) == 3 and second.footer == "Page 2 / 2"
    assert second.buttons[1].disabled and second.buttons[0].custom_id == "t12a:wl:page:bluesky:0"
    assert seen[-1]["platform"] == "bluesky"
    assert second.fields[0].control.options[2].default  # type: ignore[union-attr]
    assert_persistent(second)


async def test_watchlist_forms_validate_before_calling_gate() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("gate must not be called")

    gate = mock_gate(handler)
    bad = await watchlist.add_form(gate, 1, "en", "x", "someone", "journalist", "500")
    assert bad.accent == "warning" and "between -100 and 100" in bad.text
    bad = await watchlist.edit_form(gate, 1, "fr", "00000000", None, "abc")
    assert bad.accent == "warning"
    nothing = await watchlist.edit_form(gate, 1, "en", "00000000")
    assert nothing.title == "Nothing to change"
    assert watchlist.parse_priority("") == 0 and watchlist.parse_priority("-100") == -100


async def test_case_card_buttons_follow_the_status() -> None:
    pid = "11111111-2222-3333-4444-555555555555"
    states = {
        "received": [False, True, False, True],
        "in_review": [True, False, False, True],
        "validated": [True, True, True, False],
        "published": [True, True, True, True],
        "rejected": [True, True, True, True],
    }
    for status, disabled in states.items():

        def handler(request: httpx.Request, status: str = status) -> httpx.Response:
            body = {"public_id": pid, "status": status, "version": 2, "x_post_id": "42"}
            return httpx.Response(200, json=body)

        card = await cases.show(mock_gate(handler), 1, "en", pid)
        assert [b.disabled for b in card.buttons[:4]] == disabled, status
        assert card.buttons[4].url == "https://x.com/i/status/42"
        assert_persistent(card)
    assert card.accent == "neutral"


async def test_case_take_refreshes_the_card_with_the_new_status() -> None:
    pid = "11111111-2222-3333-4444-555555555555"
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "GET":
            return httpx.Response(200, json={"public_id": pid, "status": "received", "version": 1})
        return httpx.Response(200, json={"public_id": pid, "status": "in_review", "version": 2})

    r = await cases.take(mock_gate(handler), 1, "en", pid)
    assert calls == [f"GET /v1/cases/{pid}", f"POST /v1/cases/{pid}/transitions"]
    assert "in review" in r.text and r.buttons[1].disabled is False and "taken" in r.footer


async def test_incident_list_offers_a_resolve_select() -> None:
    rows = [
        {"code": "TWR-MNT-001", "title": "T", "fingerprint": "a" * 64, "occurrences": 3},
        {"code": "TWR-MNT-002", "title": "U", "fingerprint": "b" * 64, "occurrences": 1},
    ]
    gate = mock_gate(lambda request: httpx.Response(200, json=rows))
    r = await incidents.list_incidents(gate, 1, "en")
    assert len(r.items) == 2 and "x3" in r.items[0] and "a" * 12 in r.items[0]
    show, pick = (f.control for f in r.fields)
    assert show.custom_id == "t12a:inc:show" and show.options[0].value == "a" * 64  # type: ignore[union-attr]
    assert pick.custom_id == "t12a:inc:pick" and pick.options[1].value == "b" * 64  # type: ignore[union-attr]
    assert r.buttons[0].custom_id == "t12a:inc:list"
    assert_persistent(r)


async def test_incident_show_bounds_a_long_report() -> None:
    fp = "0123456789abcdef"
    report = {
        "fingerprint": fp,
        "code": "TWR-INT-001",
        "title": "Unexpected",
        "severity": "error",
        "last_message": "m```" * 400,
        "last_context": {f"k{i}": "v" * 500 for i in range(30)},
        "last_traceback": "frame\n" * 2000 + "ValueError: boom",
        "hint": "Look at the logs.",
        "retryable": False,
        "occurrences": 2,
    }
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url.copy_with(query=None)) + "?" + request.url.query.decode())
        if request.url.path == "/v1/errors":
            return httpx.Response(200, json=[report])
        return httpx.Response(200, json=report)

    r = await incidents.show(mock_gate(handler), 1, "en", fp[:8])
    assert seen == [
        "http://gate/v1/errors?limit=200&include_resolved=true",
        f"http://gate/v1/errors/{fp}?",
    ]
    assert r.accent == "error"
    assert len(r.body) < 3800 and r.body.count("```") == 6
    assert r.body.rstrip("`\n").endswith("ValueError: boom") and "... and 18 more" in r.body
    assert "Look at the logs." in r.body and "Retryable: no" in r.text


async def test_registry_buttons_and_long_domains() -> None:
    from tower.intercom.handlers import registry as rh

    entry = {
        "id": "i1",
        "domain": "example.org",
        "publisher": "Example",
        "tier": "primary",
        "kind": "press",
        "version": 3,
        "active": True,
    }
    gate = mock_gate(lambda request: httpx.Response(200, json=entry))
    r = await rh.registry_show(gate, 1, "en", "https://www.Example.org/x")
    assert [b.custom_id.split(":")[2] for b in r.buttons] == ["tier", "control", "history", "seed"]
    assert_persistent(r)
    assert rh.cid("tier", "a" * 90) == "" and rh.cid("tier", "bad_domain.org") == ""
    long_entry = {**entry, "domain": "a" * 95 + ".org"}
    gate = mock_gate(lambda request: httpx.Response(200, json=long_entry))
    assert (await rh.registry_show(gate, 1, "en", "x")).buttons == ()


async def test_level_form_rejects_a_bad_duration() -> None:
    from tower.intercom.handlers import profiles as ph

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("gate must not be called")

    r = await ph.level_set_form(mock_gate(handler), 1, "en", "content", "x:1", "N2", "soon", "r")
    assert r.accent == "warning" and "hours" in r.text


def test_modals_build_with_their_components() -> None:
    import asyncio

    from tower.intercom import commands
    from tower.intercom.commands import cases as cd
    from tower.intercom.commands import profiles as cp
    from tower.intercom.commands import registry as cr
    from tower.intercom.commands import watchlist as cw

    async def build() -> list[int]:
        ctx = commands.Context(gate=mock_gate(lambda r: httpx.Response(200)), default_locale="en")
        modals = [
            cw._kit(ctx)[2]("fr"),
            cw._kit(ctx)[3]("00000000", "en"),
            cd._kit(ctx)[1]("p", "en"),
            cd._kit(ctx)[2]("p", "fr"),
            cp._kit(ctx)[5]("fr"),
            cr._kit(ctx)[1]("example.org", "en"),
        ]
        return [len(m.children) for m in modals]

    counts = asyncio.run(build())
    assert counts == [5, 5, 1, 2, 5, 3]
