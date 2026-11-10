from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

import httpx
import orjson
import pytest
import uvicorn
from fastapi import FastAPI

from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain.security import Permission
from tower.core.errors import get_reporter
from tower.core.events.outbox import publish
from tower.core.models.errors import ErrorReport

from .conftest import TokenFactory

STAFF = 111111111111111111
POST = "1850000000000000001"


def h(token: str | None = None, staff: int | None = None) -> dict[str, str]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if staff:
        headers["X-Acting-Discord-Id"] = str(staff)
    return headers


async def test_health_and_ready(client: httpx.AsyncClient) -> None:
    assert (await client.get("/health")).json()["status"] == "ok"
    assert (await client.get("/ready")).json() == {"status": "ready"}


async def test_auth_errors(
    client: httpx.AsyncClient,
    make_token: TokenFactory,
    add_staff: Callable[..., Coroutine[Any, Any, None]],
) -> None:
    r = await client.get("/v1/dossiers", headers=h("t12a_nope"))
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "TWR-AUTH-001"
    assert r.json()["error"]["request_id"] == r.headers["x-request-id"]

    revoked = await make_token("old", revoked=True)
    assert (await client.get("/v1/dossiers", headers=h(revoked))).status_code == 401

    r = await client.get("/v1/dossiers", headers=h(None, STAFF))
    assert r.json()["error"]["code"] == "TWR-AUTH-002"

    token = await make_token()
    r = await client.get("/v1/dossiers", headers=h(token, STAFF))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "TWR-AUTH-004"

    await add_staff(STAFF, ["reviewer"], active=False)
    assert (await client.get("/v1/dossiers", headers=h(token, STAFF))).status_code == 403


async def test_dossier_lifecycle(
    client: httpx.AsyncClient,
    make_token: TokenFactory,
    add_staff: Callable[..., Coroutine[Any, Any, None]],
) -> None:
    token = await make_token()
    await add_staff(STAFF, ["editor"])
    staff = h(token, STAFF)

    r = await client.post(
        "/v1/dossiers", json={"kind": "fact_check", "x_post_id": POST}, headers=staff
    )
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["status"] == "received" and d["x_post_id"] == POST
    pid = d["public_id"]

    # The public does not see an unpublished dossier.
    assert (await client.get(f"/v1/dossiers/{pid}")).json()["error"]["code"] == "TWR-DOS-001"

    r = await client.post(
        f"/v1/dossiers/{pid}/transitions", json={"to": "published"}, headers=staff
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "TWR-DOS-002"
    assert r.json()["error"]["details"]["allowed"] == ["in_review"]

    r = await client.post(
        f"/v1/dossiers/{pid}/transitions",
        json={"to": "in_review", "expected_version": 1},
        headers=staff,
    )
    assert r.json()["assigned_to"] == str(STAFF)
    r = await client.post(
        f"/v1/dossiers/{pid}/transitions",
        json={"to": "validated", "expected_version": 1},
        headers=staff,
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "TWR-GEN-003"
    await client.post(f"/v1/dossiers/{pid}/transitions", json={"to": "validated"}, headers=staff)

    r = await client.post(f"/v1/dossiers/{pid}/publish", json={"url": "https://bad"}, headers=staff)
    assert r.json()["error"]["code"] == "TWR-DOS-003"
    url = "https://x.com/seat12a/status/1850000000000000999"
    r = await client.post(f"/v1/dossiers/{pid}/publish", json={"url": url}, headers=staff)
    assert r.json()["status"] == "published" and r.json()["published_url"] == url

    public = (await client.get(f"/v1/dossiers/{pid}")).json()
    assert set(public) == {"public_id", "kind", "x_post_id", "title", "published_url", "updated_at"}

    history = (await client.get(f"/v1/dossiers/{pid}/history", headers=staff)).json()
    assert [e["to_status"] for e in history] == ["received", "in_review", "validated", "published"]
    assert {e["actor"] for e in history} == {f"staff:{STAFF}@intercom"}


async def test_reviewer_cannot_validate(
    client: httpx.AsyncClient,
    make_token: TokenFactory,
    add_staff: Callable[..., Coroutine[Any, Any, None]],
) -> None:
    token = await make_token()
    await add_staff(STAFF, ["reviewer"])
    staff = h(token, STAFF)
    pid = (await client.post("/v1/dossiers", json={"kind": "moderation"}, headers=staff)).json()[
        "public_id"
    ]
    await client.post(f"/v1/dossiers/{pid}/transitions", json={"to": "in_review"}, headers=staff)
    r = await client.post(
        f"/v1/dossiers/{pid}/transitions", json={"to": "validated"}, headers=staff
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "TWR-AUTH-005"


async def test_validation_error_format(client: httpx.AsyncClient, make_token: TokenFactory) -> None:
    token = await make_token()
    r = await client.post("/v1/dossiers", json={"kind": "nope", "x_post_id": 12}, headers=h(token))
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "TWR-GEN-001"
    assert {e["field"] for e in err["details"]["errors"]} == {"body.kind", "body.x_post_id"}
    r = await client.get("/v1/nonexistent")
    assert r.status_code == 404 and r.json()["error"]["code"] == "TWR-GEN-002"


async def test_unexpected_error_is_stored(client: httpx.AsyncClient, db: Database) -> None:
    app: FastAPI = client._transport.app  # type: ignore[attr-defined]

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("unexpected outage")

    r = await client.get("/boom")
    assert r.status_code == 500
    err = r.json()["error"]
    assert err["code"] == "TWR-INT-001" and err["message"] == "Internal error"
    assert "outage" not in r.text  # no leak of the detail
    await get_reporter().flush()
    async with db.session() as s:
        report = await s.get(ErrorReport, err["fingerprint"])
    assert report is not None and report.last_context["request_id"] == err["request_id"]


async def test_errors_api(
    client: httpx.AsyncClient, make_token: TokenFactory, db: Database
) -> None:
    from tower.core.domain import error_codes as ec

    fp = get_reporter().capture(ec.MNT_TASK_FAILED, RuntimeError("x"))
    await get_reporter().flush()
    admin = await make_token("admin-tool")
    limited = await make_token("site", scopes=[Permission.DOSSIER_READ])
    assert (await client.get("/v1/errors", headers=h(limited))).status_code == 403
    rows = (await client.get("/v1/errors", headers=h(admin))).json()
    assert [r["fingerprint"] for r in rows] == [fp]
    detail = (await client.get(f"/v1/errors/{fp}", headers=h(admin))).json()
    assert "RuntimeError" in detail["last_traceback"]
    r = await client.post(f"/v1/errors/{fp}/resolve", json={"note": "ok"}, headers=h(admin))
    assert r.json()["resolved_by"] == "service:admin-tool"
    assert (await client.get("/v1/errors", headers=h(admin))).json() == []
    catalog = (await client.get("/v1/errors/catalog")).json()
    assert any(c["code"] == "TWR-DOS-002" for c in catalog)


async def _read_sse(response: httpx.Response, count: int) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    current: dict[str, str] = {}
    async with asyncio.timeout(5):
        async for line in response.aiter_lines():
            if not line:
                if "data" in current:
                    events.append(current)
                    if len(events) == count:
                        return events
                current = {}
                continue
            if line.startswith(":"):
                continue
            key, _, value = line.partition(": ")
            current[key] = value
    return events


@pytest.fixture
async def live_url(settings: Settings, db: Database) -> AsyncIterator[str]:
    """Real uvicorn server: ASGITransport cannot stream an endless response."""
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


async def test_sse_stream_and_resume(live_url: str, make_token: TokenFactory, db: Database) -> None:
    token = await make_token()
    async with httpx.AsyncClient(base_url=live_url, timeout=5) as client:
        anon = await client.get("/v1/events", params={"after": "0"})
        assert anon.status_code == 403

        async with db.transaction() as s:
            await publish(s, "test.one", {"n": 1})
            await publish(s, "test.two", {"n": 2})

        async with client.stream("GET", "/v1/events", params={"after": "0"}, headers=h(token)) as r:
            assert r.headers["content-type"].startswith("text/event-stream")
            first = await _read_sse(r, 1)
        assert first[0]["event"] == "test.one"

        async def later() -> None:
            await asyncio.sleep(0.3)
            async with db.transaction() as s:
                await publish(s, "test.three", {"n": 3})

        task = asyncio.create_task(later())
        resume = {**h(token), "Last-Event-ID": first[0]["id"]}
        async with client.stream("GET", "/v1/events", headers=resume) as r:
            rest = await _read_sse(r, 2)
        await task
    assert [e["event"] for e in rest] == ["test.two", "test.three"]
    assert orjson.loads(rest[1]["data"])["payload"] == {"n": 3}
