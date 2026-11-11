from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

import httpx
import pytest

from tower.core.db.database import Database

from .conftest import TokenFactory
from .test_drafts_usecases import EXT, build_draft

REVIEWER = 1001
EDITOR = 1002
UNKNOWN = "11111111-2222-3333-4444-555555555555"
AddStaff = Callable[..., Coroutine[Any, Any, None]]


def h(token: str, staff: int | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if staff:
        headers["X-Acting-Discord-Id"] = str(staff)
    return headers


@pytest.fixture
async def bot(make_token: TokenFactory, add_staff: AddStaff) -> str:
    token = await make_token("intercom")
    await add_staff(REVIEWER, ["reviewer"])
    await add_staff(EDITOR, ["editor"])
    return token


async def test_request_factcheck_round_trip(client: httpx.AsyncClient, bot: str) -> None:
    body = {"url": f"https://x.com/someone/status/{EXT}"}
    r = await client.post("/v1/factcheck/requests", json=body, headers=h(bot, EDITOR))
    assert r.status_code == 202
    assert r.json() == {"platform": "x", "external_id": EXT, "accepted": True, "duplicate": False}
    again = await client.post(
        "/v1/factcheck/requests",
        json={"platform": "x", "external_id": EXT},
        headers=h(bot, EDITOR),
    )
    assert again.status_code == 202 and again.json()["duplicate"] is True


async def test_request_factcheck_errors(client: httpx.AsyncClient, bot: str) -> None:
    r = await client.post(
        "/v1/factcheck/requests", json={"url": "https://example.com/a"}, headers=h(bot, EDITOR)
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "TWR-FCK-012"
    r = await client.post(
        "/v1/factcheck/requests",
        json={"url": f"https://x.com/a/status/{EXT}"},
        headers=h(bot, REVIEWER),
    )
    assert r.status_code == 403
    r = await client.post("/v1/factcheck/requests", json={"url": f"https://x.com/a/status/{EXT}"})
    assert r.status_code in (401, 403)


async def test_draft_queue_and_review_flow(
    client: httpx.AsyncClient, bot: str, db: Database
) -> None:
    built = await build_draft(db)
    await build_draft(db, ext="1850000000000000099", drafted=False)

    r = await client.get("/v1/drafts?status=pending&notified=false", headers=h(bot, REVIEWER))
    assert r.status_code == 200
    rows = r.json()
    assert [d["draft_id"] for d in rows] == [str(built.verdict)]
    assert rows[0]["claim"]["text_fr"] and rows[0]["content"]["external_id"] == EXT

    one = await client.get(f"/v1/drafts/{built.verdict}", headers=h(bot, REVIEWER))
    assert (
        one.status_code == 200
        and one.json()["rationale"][0]["citations"][0]["source"]["publisher"] == "INSEE"
    )
    missing = await client.get(f"/v1/drafts/{UNKNOWN}", headers=h(bot, REVIEWER))
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "TWR-FCK-007"

    assert (
        await client.post(f"/v1/drafts/{built.verdict}/notified", headers=h(bot, REVIEWER))
    ).status_code == 204
    assert (
        await client.post(f"/v1/drafts/{built.verdict}/notified", headers=h(bot, REVIEWER))
    ).status_code == 204
    r = await client.get("/v1/drafts?notified=false", headers=h(bot, REVIEWER))
    assert r.json() == []
    r = await client.get("/v1/drafts?notified=true", headers=h(bot, REVIEWER))
    assert len(r.json()) == 1

    denied = await client.post(
        f"/v1/drafts/{built.verdict}/review",
        json={"decision": "reject", "reason": "no"},
        headers=h(bot, REVIEWER),
    )
    assert denied.status_code == 403
    invalid = await client.post(
        f"/v1/drafts/{built.verdict}/review", json={"decision": "edit"}, headers=h(bot, EDITOR)
    )
    assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "TWR-FCK-009"
    stale = await client.post(
        f"/v1/drafts/{built.verdict}/review",
        json={"decision": "accept", "expected_version": 7},
        headers=h(bot, EDITOR),
    )
    assert stale.status_code == 409

    endorsed = await client.post(
        f"/v1/drafts/{built.verdict}/review", json={"decision": "accept"}, headers=h(bot, REVIEWER)
    )
    assert endorsed.status_code == 200 and endorsed.json()["outcome"] == "endorsed"

    r = await client.post(
        f"/v1/drafts/{built.verdict}/review",
        json={
            "decision": "edit",
            "veracity": "misleading",
            "note_text": "Nouvelle note",
            "labels": ["wrong_veracity"],
        },
        headers=h(bot, EDITOR),
    )
    assert r.status_code == 200
    out = r.json()
    assert out["outcome"] == "validated" and out["draft"]["status"] == "active"
    assert out["draft"]["veracity"] == "misleading"
    assert out["draft"]["notes"]["fr"]["origin"] == "human"

    again = await client.post(
        f"/v1/drafts/{built.verdict}/review", json={"decision": "accept"}, headers=h(bot, EDITOR)
    )
    assert again.status_code == 409 and again.json()["error"]["code"] == "TWR-FCK-008"
    assert (await client.get("/v1/drafts", headers=h(bot, REVIEWER))).json() == []


async def test_draft_endpoints_need_the_read_permission(
    client: httpx.AsyncClient, bot: str
) -> None:
    assert (await client.get("/v1/drafts")).status_code in (401, 403)
    r = await client.get("/v1/drafts?status=archived", headers=h(bot, REVIEWER))
    assert r.status_code == 422


async def test_public_verdict_endpoint(client: httpx.AsyncClient, bot: str, db: Database) -> None:
    built = await build_draft(db, post_lang="en")
    url = f"/v1/content/x/{EXT}/verdict"
    assert (await client.get(url)).status_code == 404  # a draft is never public
    r = await client.post(
        f"/v1/drafts/{built.verdict}/review", json={"decision": "accept"}, headers=h(bot, EDITOR)
    )
    assert r.status_code == 200

    anonymous = await client.get(url)
    assert anonymous.status_code == 200
    assert anonymous.headers["cache-control"] == "public, max-age=300"
    body = anonymous.json()
    assert body["platform"] == "x" and body["external_id"] == EXT and body["lang"] == "en"
    claim = body["claims"][0]
    assert claim["veracity"] == "false" and claim["note"]["lang"] == "en"
    assert claim["note"]["machine_translated"] is False
    assert claim["text"] == "Unemployment doubled in a year"

    french = (await client.get(url, params={"lang": "fr"})).json()["claims"][0]
    assert french["note"]["lang"] == "fr" and french["text"] == "Le chômage a doublé en un an"
    missing = await client.get("/v1/content/x/123/verdict")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "TWR-FCK-013"
    assert "cache-control" not in missing.headers


async def test_openapi_tags(client: httpx.AsyncClient) -> None:
    spec = (await client.get("/openapi.json")).json()
    tags = {
        path: {t for op in item.values() for t in op.get("tags", [])}
        for path, item in spec["paths"].items()
    }
    assert tags["/v1/factcheck/requests"] == {"factcheck"}
    assert tags["/v1/drafts"] == {"drafts"}
    assert tags["/v1/content/{platform}/{external_id}/verdict"] == {"public"}
