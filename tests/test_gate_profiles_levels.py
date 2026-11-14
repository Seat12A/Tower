from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

import httpx
import pytest

from tower.core.db.database import Database
from tower.core.factcheck.profiles import invalidate_profile_cache

from .conftest import TokenFactory
from .test_drafts_usecases import build_draft

REVIEWER = 1001
EDITOR = 1002
ADMIN = 1003
AddStaff = Callable[..., Coroutine[Any, Any, None]]


def h(token: str, staff: int | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if staff:
        headers["X-Acting-Discord-Id"] = str(staff)
    return headers


@pytest.fixture
async def bot(make_token: TokenFactory, add_staff: AddStaff) -> str:
    invalidate_profile_cache()
    token = await make_token("intercom")
    await add_staff(REVIEWER, ["reviewer"])
    await add_staff(EDITOR, ["editor"])
    await add_staff(ADMIN, ["admin"])
    return token


async def test_profiles_read_and_default(client: httpx.AsyncClient, bot: str) -> None:
    for staff in (REVIEWER, EDITOR, ADMIN):
        r = await client.get("/v1/profiles/active", headers=h(bot, staff))
        assert r.status_code == 200
        assert r.json()["version"] == 0 and "levels" in r.json()["settings"]
    r = await client.get("/v1/profiles", headers=h(bot, REVIEWER))
    assert r.status_code == 200
    assert r.json() == [
        {
            "version": 0,
            "active": True,
            "author": "system",
            "reason": "Default profile built from the settings",
            "created_at": None,
            "activated_at": None,
            "based_on_version": None,
        }
    ]
    assert (await client.get("/v1/profiles")).status_code in (401, 403)


async def test_profile_create_activate_rollback(client: httpx.AsyncClient, bot: str) -> None:
    body = {"changes": {"draft_daily_quota": 40}, "reason": "more drafts", "activate": True}
    r = await client.post("/v1/profiles", json=body, headers=h(bot, ADMIN))
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 1 and r.json()["active"] is True
    active = await client.get("/v1/profiles/active", headers=h(bot, EDITOR))
    assert active.json()["version"] == 1 and active.json()["settings"]["draft_daily_quota"] == 40

    r = await client.post(
        "/v1/profiles",
        json={"changes": {"draft_daily_quota": 41}, "reason": "later"},
        headers=h(bot, ADMIN),
    )
    assert r.status_code == 201 and r.json()["active"] is False
    r = await client.post("/v1/profiles/2/activate", json={"reason": "go"}, headers=h(bot, ADMIN))
    assert r.status_code == 200 and r.json()["version"] == 2
    r = await client.post("/v1/profiles/rollback", json={"reason": "back"}, headers=h(bot, ADMIN))
    assert r.status_code == 200 and r.json()["version"] == 1
    listing = await client.get("/v1/profiles", headers=h(bot, ADMIN))
    assert [(v["version"], v["active"]) for v in listing.json()] == [
        (2, False),
        (1, True),
        (0, False),
    ]
    r = await client.post(
        "/v1/profiles/0/activate", json={"reason": "reset"}, headers=h(bot, ADMIN)
    )
    assert r.status_code == 200 and r.json()["version"] == 0
    invalidate_profile_cache()


async def test_profile_errors(client: httpx.AsyncClient, bot: str) -> None:
    r = await client.post(
        "/v1/profiles",
        json={"changes": {"draft_daily_quota": 9999}, "reason": "x"},
        headers=h(bot, ADMIN),
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "TWR-FCK-030"
    assert r.json()["error"]["details"]["violations"]
    r = await client.post(
        "/v1/profiles", json={"changes": {"nope": 1}, "reason": "x"}, headers=h(bot, ADMIN)
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "TWR-FCK-030"
    r = await client.post("/v1/profiles/77/activate", json={"reason": "x"}, headers=h(bot, ADMIN))
    assert r.status_code == 404 and r.json()["error"]["code"] == "TWR-FCK-029"


@pytest.mark.parametrize("staff", [REVIEWER, EDITOR])
async def test_profile_writes_need_admin(client: httpx.AsyncClient, bot: str, staff: int) -> None:
    for path, body in (
        ("/v1/profiles", {"changes": {"draft_daily_quota": 1}, "reason": "x"}),
        ("/v1/profiles/0/activate", {"reason": "x"}),
        ("/v1/profiles/rollback", {"reason": "x"}),
    ):
        r = await client.post(path, json=body, headers=h(bot, staff))
        assert r.status_code == 403, path


async def test_level_overrides_round_trip(
    client: httpx.AsyncClient, bot: str, db: Database
) -> None:
    built = await build_draft(db)
    r = await client.post(
        "/v1/level-overrides",
        json={
            "target_type": "content",
            "target": "x:1850000000000000042",
            "level": "N4",
            "hours": 12,
            "reason": "viral",
        },
        headers=h(bot, EDITOR),
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["applied"] is True and created["level"] == "N4"
    assert created["target"] == "1850000000000000042" and created["author"].startswith("staff:")
    assert built.group_id

    listed = await client.get("/v1/level-overrides?active=true", headers=h(bot, REVIEWER))
    assert listed.status_code == 200
    (row,) = listed.json()
    assert row["id"] == created["id"] and row["applied"] is None

    r = await client.post(
        f"/v1/level-overrides/{created['id']}/revoke",
        json={"reason": "calm"},
        headers=h(bot, EDITOR),
    )
    assert r.status_code == 200 and r.json()["revoked_by"].startswith("staff:")
    assert (await client.get("/v1/level-overrides", headers=h(bot, EDITOR))).json() == []
    all_rows = await client.get("/v1/level-overrides?active=false", headers=h(bot, EDITOR))
    assert len(all_rows.json()) == 1


async def test_level_override_errors(client: httpx.AsyncClient, bot: str) -> None:
    body = {"target_type": "group", "target": "x", "level": "N2", "hours": 1, "reason": "r"}
    r = await client.post("/v1/level-overrides", json=body, headers=h(bot, EDITOR))
    assert r.status_code == 422 and r.json()["error"]["code"] == "TWR-FCK-031"
    r = await client.post(
        "/v1/level-overrides",
        json={**body, "target": "11111111-2222-3333-4444-555555555555"},
        headers=h(bot, EDITOR),
    )
    assert r.status_code == 404 and r.json()["error"]["code"] == "TWR-FCK-032"
    r = await client.post(
        "/v1/level-overrides", json={**body, "hours": 9999}, headers=h(bot, EDITOR)
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "TWR-FCK-031"
    r = await client.post("/v1/level-overrides", json=body, headers=h(bot, REVIEWER))
    assert r.status_code == 403
    r = await client.post(
        "/v1/level-overrides/11111111-2222-3333-4444-555555555555/revoke",
        json={"reason": "x"},
        headers=h(bot, EDITOR),
    )
    assert r.status_code == 404 and r.json()["error"]["code"] == "TWR-FCK-032"
    r = await client.post(
        "/v1/level-overrides/11111111-2222-3333-4444-555555555555/revoke",
        json={"reason": "x"},
        headers=h(bot, REVIEWER),
    )
    assert r.status_code == 403
