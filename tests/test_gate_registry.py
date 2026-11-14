"""Gate endpoints of the reliability registry and the country registries."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from tower.core.db.database import Database
from tower.core.domain import error_codes as ec

from .conftest import TokenFactory

REVIEWER = 1001
EDITOR = 1002
AddStaff = Callable[..., Coroutine[Any, Any, None]]

BODY: dict[str, Any] = {
    "domain": "https://www.example.org/",
    "publisher": "Example",
    "tier": "secondary",
    "kind": "press",
    "country": "FR",
    "reason": "Public correction policy",
}


def h(token: str, staff: int) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Acting-Discord-Id": str(staff)}


@pytest.fixture
async def bot(make_token: TokenFactory, add_staff: AddStaff, db: Database) -> str:
    async with db.transaction() as s:
        await s.execute(
            text("TRUNCATE jurisdiction_sources, legal_portals, election_calendar, jobs")
        )
    token = await make_token("intercom")
    await add_staff(REVIEWER, ["reviewer"])
    await add_staff(EDITOR, ["editor"])
    return token


async def create(client: httpx.AsyncClient, token: str, **over: Any) -> dict[str, Any]:
    r = await client.post("/v1/registry", json={**BODY, **over}, headers=h(token, EDITOR))
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


async def test_crud_flow(client: httpx.AsyncClient, bot: str) -> None:
    created = await create(client, bot)
    assert created["domain"] == "example.org" and created["version"] == 1
    assert isinstance(created["id"], str)

    r = await client.get("/v1/registry/example.org", headers=h(bot, REVIEWER))
    assert r.status_code == 200 and r.json()["id"] == created["id"]
    r = await client.get(f"/v1/registry/{created['id']}", headers=h(bot, REVIEWER))
    assert r.json()["domain"] == "example.org"

    r = await client.patch(
        f"/v1/registry/{created['id']}",
        json={"version": 1, "reason": "Official producer", "tier": "primary"},
        headers=h(bot, EDITOR),
    )
    assert r.status_code == 200, r.text
    assert (r.json()["tier"], r.json()["version"]) == ("primary", 2)

    r = await client.get("/v1/registry/example.org/history", headers=h(bot, REVIEWER))
    assert [v["version"] for v in r.json()] == [2, 1]
    assert r.json()[0]["reason"] == "Official producer"

    r = await client.get("/v1/registry?country=FR&tier=primary&q=exam", headers=h(bot, REVIEWER))
    assert [e["domain"] for e in r.json()] == ["example.org"]

    r = await client.post(
        f"/v1/registry/{created['id']}/deactivate",
        json={"version": 2, "reason": "Defunct"},
        headers=h(bot, EDITOR),
    )
    assert r.status_code == 200 and r.json()["active"] is False
    r = await client.get("/v1/registry/export", headers=h(bot, REVIEWER))
    assert r.status_code == 200 and r.json()["count"] == 0


async def test_export_lists_active_entries(client: httpx.AsyncClient, bot: str) -> None:
    await create(client, bot)
    r = await client.get("/v1/registry/export", headers=h(bot, REVIEWER))
    body = r.json()
    assert body["count"] == 1 and body["entries"][0]["domain"] == "example.org"
    assert len(body["digest"]) == 64


async def test_forbidden_for_reviewer_and_unknown_staff(
    client: httpx.AsyncClient, bot: str
) -> None:
    r = await client.post("/v1/registry", json=BODY, headers=h(bot, REVIEWER))
    assert r.status_code == 403
    r = await client.put(
        "/v1/countries/FR/elections",
        json={"kind": "local", "name": "n", "first_round": "2030-03-01"},
        headers=h(bot, REVIEWER),
    )
    assert r.status_code == 403
    r = await client.get("/v1/registry", headers=h(bot, 9999))
    assert r.status_code in (401, 403)


async def test_not_found_conflict_invalid(client: httpx.AsyncClient, bot: str) -> None:
    r = await client.get("/v1/registry/nope.org", headers=h(bot, REVIEWER))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == ec.ATL_REGISTRY_NOT_FOUND.code
    created = await create(client, bot)
    r = await client.patch(
        f"/v1/registry/{created['id']}",
        json={"version": 5, "reason": "x", "tier": "primary"},
        headers=h(bot, EDITOR),
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == ec.ATL_REGISTRY_CONFLICT.code
    r = await client.post("/v1/registry", json=BODY, headers=h(bot, EDITOR))
    assert r.status_code == 409  # duplicate domain
    r = await client.post(
        "/v1/registry", json={**BODY, "domain": "other.org", "tier": "gold"}, headers=h(bot, EDITOR)
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == ec.ATL_REGISTRY_INVALID.code
    r = await client.patch(
        f"/v1/registry/{created['id']}",
        json={"version": 1, "reason": " ", "tier": "primary"},
        headers=h(bot, EDITOR),
    )
    assert r.status_code == 422
    r = await client.patch(
        f"/v1/registry/{created['id']}",
        json={"version": 1, "reason": "x", "statutory_independence": True},
        headers=h(bot, EDITOR),
    )
    assert r.status_code == 422


async def test_seed_control_enqueues_once(
    client: httpx.AsyncClient, bot: str, db: Database
) -> None:
    created = await create(client, bot)
    r1 = await client.post(f"/v1/registry/{created['id']}/seed-control", headers=h(bot, EDITOR))
    r2 = await client.post("/v1/registry/example.org/seed-control", headers=h(bot, EDITOR))
    assert (r1.status_code, r1.json()) == (202, {"enqueued": True})
    assert r2.json() == {"enqueued": False}
    r = await client.post("/v1/registry/example.org/seed-control", headers=h(bot, REVIEWER))
    assert r.status_code == 403
    async with db.session() as s:
        count = (
            await s.execute(text("SELECT count(*) FROM jobs WHERE kind = 'atlas.control_seed'"))
        ).scalar_one()
    assert count == 1


async def test_country_registries(client: httpx.AsyncClient, bot: str) -> None:
    source = {"source_type": "statistics", "key": "insee", "name": "INSEE", "domain": "insee.fr"}
    r = await client.put("/v1/countries/FR/sources", json=source, headers=h(bot, EDITOR))
    assert r.status_code == 200 and r.json()["version"] == 1
    r = await client.put(
        "/v1/countries/FR/sources", json={**source, "name": "Insee"}, headers=h(bot, EDITOR)
    )
    assert r.json()["version"] == 2
    r = await client.put(
        "/v1/countries/FR/sources", json={**source, "version": 1}, headers=h(bot, EDITOR)
    )
    assert r.status_code == 409
    r = await client.get("/v1/countries/FR/sources", headers=h(bot, REVIEWER))
    assert [s["name"] for s in r.json()] == ["Insee"]
    r = await client.put("/v1/countries/fr/sources", json=source, headers=h(bot, EDITOR))
    assert r.status_code == 422

    portal = {"name": "Fedlex", "url": "https://www.fedlex.admin.ch/", "access": "api", "eli": True}
    r = await client.put("/v1/countries/CH/legal-portals", json=portal, headers=h(bot, EDITOR))
    assert r.status_code == 200 and r.json()["domain"] == "fedlex.admin.ch"
    r = await client.get("/v1/countries/CH/legal-portals", headers=h(bot, REVIEWER))
    assert len(r.json()) == 1

    election = {"kind": "local", "name": "Test", "first_round": "2030-03-01"}
    r = await client.put("/v1/countries/FR/elections", json=election, headers=h(bot, EDITOR))
    assert r.status_code == 200 and r.json()["first_round"] == "2030-03-01"
    r = await client.get(
        "/v1/countries/FR/elections?upcoming_from=2031-01-01", headers=h(bot, REVIEWER)
    )
    assert r.json() == []
