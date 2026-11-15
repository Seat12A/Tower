"""Gate endpoints of the team: ranks and individual permissions (needs PostgreSQL)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from tower.core.db.database import Database
from tower.core.domain.security import Permission

from .conftest import TokenFactory, make_settings

ADMIN = 9000
MANAGER = 9001  # staff:read, staff:manage, permissions:manage_others + usage:read
PLAIN = 9002
OTHER = 9003


def h(token: str, staff: int | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if staff is not None:
        headers["X-Acting-Discord-Id"] = str(staff)
    return headers


@pytest.fixture
async def api(db: Database) -> AsyncIterator[httpx.AsyncClient]:
    from tower.gate.app import create_app

    app = create_app(make_settings(admin_discord_ids=[ADMIN]))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gate") as c:
            yield c


@pytest.fixture
async def bot(make_token: TokenFactory, db: Database) -> str:
    token = await make_token("intercom")
    async with db.transaction() as s:
        await s.execute(
            text(
                "INSERT INTO staff_members (discord_id, display_name, permissions) VALUES "
                "(:m, 'Manager', ARRAY['staff:read','staff:manage',"
                "'permissions:manage_others','usage:read']), "
                "(:p, 'Plain', ARRAY[]::text[])"
            ),
            {"m": MANAGER, "p": PLAIN},
        )
    return token


async def put_permissions(
    api: httpx.AsyncClient, token: str, actor: int, target: int, perms: list[str]
) -> httpx.Response:
    return await api.put(
        f"/v1/staff/{target}/permissions", json={"permissions": perms}, headers=h(token, actor)
    )


async def test_hard_coded_admin_is_resolved_without_a_row(api: httpx.AsyncClient, bot: str) -> None:
    r = await api.get("/v1/staff/me", headers=h(bot, ADMIN))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_admin"] and body["discord_id"] == str(ADMIN)
    assert Permission.PERMISSIONS_MANAGE_SELF.value in body["effective_permissions"]
    assert Permission.STAFF_MANAGE.value in body["actor_permissions"]


async def test_unknown_member_is_refused(api: httpx.AsyncClient, bot: str) -> None:
    r = await api.get("/v1/staff/me", headers=h(bot, OTHER))
    assert r.status_code == 403 and r.json()["error"]["code"] == "TWR-AUTH-004"


async def test_me_for_a_member_and_without_acting_id(api: httpx.AsyncClient, bot: str) -> None:
    r = await api.get("/v1/staff/me", headers=h(bot, MANAGER))
    assert r.status_code == 200 and "usage:read" in r.json()["permissions"]
    assert (await api.get("/v1/staff/me", headers=h(bot))).status_code == 403


async def test_list_and_get_need_staff_read(api: httpx.AsyncClient, bot: str) -> None:
    denied = await api.get("/v1/staff", headers=h(bot, PLAIN))
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "TWR-AUTH-005"
    r = await api.get("/v1/staff", headers=h(bot, MANAGER))
    ids = [m["discord_id"] for m in r.json()["items"]]
    assert ids[0] == str(ADMIN) and str(MANAGER) in ids and str(PLAIN) in ids
    own = await api.get(f"/v1/staff/{PLAIN}", headers=h(bot, PLAIN))
    assert own.status_code == 200
    other = await api.get(f"/v1/staff/{MANAGER}", headers=h(bot, PLAIN))
    assert other.status_code == 403
    missing = await api.get(f"/v1/staff/{OTHER}", headers=h(bot, MANAGER))
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "TWR-STF-001"


async def test_rank_unrank_flow(api: httpx.AsyncClient, bot: str) -> None:
    r = await api.put(
        f"/v1/staff/{OTHER}", json={"display_name": "Newbie"}, headers=h(bot, MANAGER)
    )
    assert r.status_code == 200 and r.json()["active"] and r.json()["permissions"] == []
    again = await api.put(f"/v1/staff/{OTHER}", json={}, headers=h(bot, MANAGER))
    assert again.status_code == 409 and again.json()["error"]["code"] == "TWR-STF-002"
    assert (await api.delete(f"/v1/staff/{OTHER}", headers=h(bot, MANAGER))).status_code == 204
    gone = await api.delete(f"/v1/staff/{OTHER}", headers=h(bot, MANAGER))
    assert gone.status_code == 404 and gone.json()["error"]["code"] == "TWR-STF-001"
    back = await api.put(f"/v1/staff/{OTHER}", json={}, headers=h(bot, MANAGER))
    assert back.status_code == 200 and back.json()["display_name"] == "Newbie"
    inactive = await api.get("/v1/staff?include_inactive=true", headers=h(bot, MANAGER))
    assert inactive.status_code == 200


async def test_rank_rules_self_admin_and_permission(api: httpx.AsyncClient, bot: str) -> None:
    me = await api.put(f"/v1/staff/{MANAGER}", json={}, headers=h(bot, MANAGER))
    assert me.status_code == 403 and me.json()["error"]["code"] == "TWR-STF-004"
    admin = await api.put(f"/v1/staff/{ADMIN}", json={}, headers=h(bot, MANAGER))
    assert admin.status_code == 409 and admin.json()["error"]["code"] == "TWR-STF-003"
    admin = await api.delete(f"/v1/staff/{ADMIN}", headers=h(bot, MANAGER))
    assert admin.json()["error"]["code"] == "TWR-STF-003"
    denied = await api.put(f"/v1/staff/{OTHER}", json={}, headers=h(bot, PLAIN))
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "TWR-AUTH-005"


async def test_set_permissions_materialises_roles_and_publishes(
    api: httpx.AsyncClient, bot: str, db: Database
) -> None:
    async with db.transaction() as s:
        await s.execute(
            text("UPDATE staff_members SET roles = ARRAY['reviewer'] WHERE discord_id = :p"),
            {"p": PLAIN},
        )
    r = await put_permissions(api, bot, ADMIN, PLAIN, ["usage:read", "errors:read"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["roles"] == [] and body["permissions"] == ["errors:read", "usage:read"]
    assert body["effective_permissions"] == ["errors:read", "usage:read"]
    async with db.session() as s:
        topics = (await s.execute(text("SELECT topic, payload FROM events"))).all()
    changed = [p for t, p in topics if t == "staff.permissions_changed"]
    assert len(changed) == 1 and "usage:read" in changed[0]["added"]
    assert "dossiers:review" in changed[0]["removed"]


async def test_set_permissions_rules(api: httpx.AsyncClient, bot: str) -> None:
    # Only ranked members receive permissions.
    r = await put_permissions(api, bot, MANAGER, OTHER, ["usage:read"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "TWR-STF-005"
    # Hard-coded administrators cannot be edited.
    r = await put_permissions(api, bot, MANAGER, ADMIN, [])
    assert r.json()["error"]["code"] == "TWR-STF-003"
    # Unknown and non-grantable permissions.
    for bad in ("nope", "events:read"):
        r = await put_permissions(api, bot, MANAGER, PLAIN, [bad])
        assert r.status_code == 422 and r.json()["error"]["code"] == "TWR-STF-007"
    # Anti-escalation: the manager holds usage:read but not errors:manage.
    r = await put_permissions(api, bot, MANAGER, PLAIN, ["usage:read", "errors:manage"])
    assert r.status_code == 403 and r.json()["error"]["code"] == "TWR-STF-006"
    ok = await put_permissions(api, bot, MANAGER, PLAIN, ["usage:read"])
    assert ok.status_code == 200
    # Own permissions need manage_self, which the manager lacks.
    own = await put_permissions(api, bot, MANAGER, MANAGER, [])
    assert own.status_code == 403 and own.json()["error"]["code"] == "TWR-AUTH-005"
    # Without manage_others a plain member cannot edit anyone.
    nope = await put_permissions(api, bot, PLAIN, MANAGER, [])
    assert nope.status_code == 403


async def test_changes_apply_immediately_to_the_target(api: httpx.AsyncClient, bot: str) -> None:
    before = await api.get("/v1/watched-accounts", headers=h(bot, PLAIN))
    assert before.status_code == 403
    assert (await put_permissions(api, bot, ADMIN, PLAIN, ["watchlist:read"])).status_code == 200
    assert (await api.get("/v1/watched-accounts", headers=h(bot, PLAIN))).status_code == 200
    me: dict[str, Any] = (await api.get("/v1/staff/me", headers=h(bot, PLAIN))).json()
    assert "watchlist:read" in me["actor_permissions"]
    assert (await api.delete(f"/v1/staff/{PLAIN}", headers=h(bot, ADMIN))).status_code == 204
    gone = await api.get("/v1/staff/me", headers=h(bot, PLAIN))
    assert gone.status_code == 403 and gone.json()["error"]["code"] == "TWR-AUTH-004"
