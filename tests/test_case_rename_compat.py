"""Expand step of the dossier -> case rename: old names keep working."""

from __future__ import annotations

import httpx

from tower.core.auth.tokens import generate_token, hash_token
from tower.core.db.database import Database
from tower.core.domain.cases import LEGACY_TOPICS, current_topic, with_legacy_topics
from tower.core.domain.security import (
    Permission,
    member_permissions,
    parse_grantable,
    to_permission,
)
from tower.core.models.auth import ServiceClient

from .conftest import TokenFactory


def test_legacy_permission_names_map_to_cases() -> None:
    assert to_permission("dossiers:read") is Permission.CASE_READ
    assert to_permission("cases:decide") is Permission.CASE_DECIDE
    assert to_permission("dossiers:unknown") is None
    assert parse_grantable(["dossiers:review"]) == {Permission.CASE_REVIEW}
    held = member_permissions([], ["dossiers:read", "dossiers:publish"], is_admin=False)
    assert {Permission.CASE_READ, Permission.CASE_PUBLISH} <= held


def test_legacy_topics() -> None:
    assert current_topic("dossier.opened") == "case.opened"
    assert current_topic("incident.opened") == "incident.opened"
    both = with_legacy_topics(["dossier.transitioned"])
    assert set(both) == {"dossier.transitioned", "case.transitioned"}
    assert set(with_legacy_topics(["case.opened"])) == {"case.opened", "dossier.opened"}
    assert set(LEGACY_TOPICS.values()) == {"case.opened", "case.transitioned"}


async def test_legacy_scope_names_stored_on_a_token_still_grant(
    client: httpx.AsyncClient, db: Database
) -> None:
    token = generate_token()
    async with db.transaction() as session:
        session.add(
            ServiceClient(name="old-site", token_hash=hash_token(token), scopes=["dossiers:read"])
        )
    r = await client.get("/v1/cases", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


async def test_legacy_dossier_paths_still_answer(
    client: httpx.AsyncClient, make_token: TokenFactory
) -> None:
    token = await make_token("site", scopes=[Permission.CASE_READ_PUBLIC])
    headers = {"Authorization": f"Bearer {token}"}
    new = await client.get("/v1/cases", headers=headers)
    old = await client.get("/v1/dossiers", headers=headers)
    assert new.status_code == old.status_code == 200
    assert new.json() == old.json()
