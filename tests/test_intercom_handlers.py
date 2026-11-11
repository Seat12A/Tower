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
from tower.intercom.handlers import Reply, dossiers, incidents, usage, watchlist

from .conftest import TokenFactory

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


async def open_dossier(gate: GateClient, acting: int = EDITOR) -> str:
    r = await gate._http.post(
        "/v1/dossiers",
        json={"kind": "fact_check", "x_post_id": POST},
        headers=gate._headers(acting),
    )
    assert r.status_code == 201, r.text
    return str(r.json()["public_id"])


# --- watchlist ---------------------------------------------------------------------


async def test_watchlist_add_list_edit_deactivate(bot: GateClient) -> None:
    r = await watchlist.add(bot, EDITOR, "en", "x", "@Some_Account", "journalist", 5, "note")
    assert r.ephemeral and "pending" in r.text and "@Some_Account" in r.text

    listed = await watchlist.list_accounts(bot, REVIEWER, "en", None)
    assert "@Some_Account" in listed.text and "resolution pending" in listed.text
    assert "journalist" in listed.text and "priority 5" in listed.text
    row = (await bot.list_watched(EDITOR))[0]
    assert row["public_id"][:8] in listed.text

    r = await watchlist.edit(bot, EDITOR, "en", row["public_id"][:8], priority=42)
    assert "Updated" in r.text
    assert (await bot.list_watched(EDITOR))[0]["priority"] == 42

    r = await watchlist.edit(bot, EDITOR, "en", row["public_id"][:8])
    assert "Nothing to change" in r.text

    r = await watchlist.deactivate(bot, EDITOR, "en", row["public_id"])
    assert "Deactivated" in r.text
    assert await bot.list_watched(EDITOR) == []
    assert (await watchlist.list_accounts(bot, EDITOR, "en", None)).text == "No watched account."


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
    assert "pending" in en.text and "en attente" in fr.text and "ajouté" in fr.text
    denied = await watchlist.add(bot, REVIEWER, "fr", "x", "gamma", "journalist")
    assert "permission" in denied.text and "TWR-AUTH-005" in denied.text


# --- dossiers ----------------------------------------------------------------------


async def test_dossier_happy_path(bot: GateClient) -> None:
    pid = await open_dossier(bot)
    shown = await dossiers.show(bot, REVIEWER, "en", pid)
    assert pid in shown.text and "received" in shown.text and POST in shown.text

    r = await dossiers.take(bot, REVIEWER, "en", pid)
    assert "taken into review" in r.text
    r = await dossiers.validate(bot, EDITOR, "en", pid)
    assert "validated" in r.text
    r = await dossiers.publish(bot, EDITOR, "fr", pid, f"https://x.com/seat12a/status/{POST}")
    assert "manuellement" not in r.text and "publiez le post vous-même" in r.text
    d = await bot.get_dossier(EDITOR, pid)
    assert d["status"] == "published" and d["published_url"].endswith(POST)


async def test_dossier_reject_and_errors(bot: GateClient) -> None:
    pid = await open_dossier(bot)
    await dossiers.take(bot, EDITOR, "en", pid)
    r = await dossiers.reject(bot, EDITOR, "en", pid, "not checkable")
    assert "rejected" in r.text
    assert (await bot.get_dossier(EDITOR, pid))["status"] == "rejected"
    r = await dossiers.take(bot, EDITOR, "en", pid)
    assert "TWR-DOS-002" in r.text and r.ephemeral

    pid2 = await open_dossier(bot)
    await dossiers.take(bot, REVIEWER, "en", pid2)
    forbidden = await dossiers.validate(bot, REVIEWER, "en", pid2)
    assert "TWR-AUTH-005" in forbidden.text and forbidden.ephemeral
    r = await dossiers.publish(bot, EDITOR, "en", pid2, "https://x.com/a/status/1")
    assert "TWR-DOS-002" in r.text
    r = await dossiers.show(bot, EDITOR, "en", "00000000-0000-0000-0000-000000000000")
    assert "TWR-DOS-001" in r.text
    await dossiers.validate(bot, EDITOR, "en", pid2)
    r = await dossiers.publish(bot, EDITOR, "en", pid2, "not a link")
    assert "TWR-DOS-003" in r.text


# --- incidents ---------------------------------------------------------------------


async def test_incidents_list_and_resolve(bot: GateClient) -> None:
    assert (await incidents.list_incidents(bot, ADMIN, "en")).text == "No open incident."
    fp = get_reporter().capture(ec.MNT_TASK_FAILED, RuntimeError("x"))
    await get_reporter().flush()
    listed = await incidents.list_incidents(bot, ADMIN, "en")
    assert ec.MNT_TASK_FAILED.code in listed.text and fp[:12] in listed.text and "x1" in listed.text
    r = await incidents.resolve(bot, ADMIN, "fr", fp[:8], "fixed")
    assert "résolu" in r.text and fp[:12] in r.text
    assert (await incidents.list_incidents(bot, ADMIN, "fr")).text == "Aucun incident ouvert."
    forbidden = await incidents.list_incidents(bot, EDITOR, "en")
    assert "TWR-AUTH-005" in forbidden.text


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
    assert "openai/big: 25% (250 / 1000 tokens)" in r.text
    assert "openai/small: no allocation" in r.text
    assert "global: $1.23 / $50.00" in r.text
    assert "provider:x: $0.01 / $10.00" in r.text
    fr = await usage.show_usage(cast(GateClient, FakeUsageGate()), 1, "fr")
    assert "25 %" in fr.text and "1.23 $" in fr.text


async def test_unavailable_reply_with_fake_gate() -> None:
    class Down:
        async def get_usage(self, acting: int) -> dict[str, Any]:
            raise GateUnavailable(0, "TWR-BOT-001", "down")

        async def list_incidents(self, acting: int, *, limit: int = 10) -> list[dict[str, Any]]:
            raise GateError(418, "TWR-XXX-999", "teapot")

    r = await usage.show_usage(cast(GateClient, Down()), 1, "en")
    assert r == Reply(r.text, ephemeral=True) and "TWR-BOT-001" in r.text
    unknown = await incidents.list_incidents(cast(GateClient, Down()), 1, "en")
    assert unknown.text == "teapot (TWR-XXX-999)"


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
        if request.url.path == "/v1/dossiers/x":
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
        await gate.get_dossier(42, "x")
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
    await gate.transition_dossier(1, "abc", to="rejected", version=3, note="why")
    await gate.publish_dossier(1, "abc", url="https://x.com/a/status/1", version=4)
    await gate.list_watched(1, active=None, platform="x", limit=7)
    await gate.put_cursor("intercom", "1-2")
    assert bodies["POST /v1/dossiers/abc/transitions"][0] == (
        '{"to":"rejected","expected_version":3,"reason":"why"}'
    )
    assert '"expected_version":4' in bodies["POST /v1/dossiers/abc/publish"][0]
    assert bodies["GET /v1/watched-accounts"][1] == "limit=7&platform=x"
    assert bodies["PUT /v1/events/cursors/intercom"][0] == '{"cursor":"1-2"}'


async def test_client_sse_parsing() -> None:
    body = (
        "retry: 3000\n\n"
        ": keepalive\n\n"
        'id: 1-1\nevent: dossier.opened\ndata: {"topic": "dossier.opened", "payload": {"a": 1}}\n\n'
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
    assert [(e.id, e.topic) for e in got] == [("1-1", "dossier.opened"), ("1-2", "incident.opened")]
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
            await publish(s, "dossier.opened", {"public_id": "p1"})
            await publish(s, "ping.done", {})
        got: list[SseEvent] = []
        async with asyncio.timeout(5):
            stream = gate.stream_events(after="0", topics=["dossier.opened"])
            async for event in stream:
                got.append(event)
                break
            await cast(Any, stream).aclose()
        assert got[0].topic == "dossier.opened" and got[0].data["payload"] == {"public_id": "p1"}
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
