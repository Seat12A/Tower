"""Short identifiers: a member may type the first characters of a public id."""

from __future__ import annotations

import uuid
from typing import Any, cast

import httpx
import pytest
from sqlalchemy import select, update

from tower.core.db.database import Database
from tower.core.domain.errors import InvalidInput
from tower.core.domain.ids import parse_ref, prefix_bounds
from tower.core.models.cases import Case
from tower.intercom.gate_client import GateClient, GateError
from tower.intercom.handlers import factcheck as fc
from tower.intercom.handlers import resolve_ref, resolve_refs

from .conftest import TokenFactory
from .test_drafts_usecases import build_draft

FULL = "df79307a-1234-4abc-8def-0123456789ab"


def test_prefix_bounds() -> None:
    low, high = prefix_bounds("DF79307A")
    assert low == uuid.UUID("df79307a-0000-0000-0000-000000000000")
    assert high == uuid.UUID("df79307a-ffff-ffff-ffff-ffffffffffff")
    assert prefix_bounds("df79307a-12")[0] == uuid.UUID("df79307a-1200-0000-0000-000000000000")
    for bad in ("df793", "xyz12345", "", "0" * 33):
        with pytest.raises(InvalidInput):
            prefix_bounds(bad)


class ResolveGate:
    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str]] = []

    async def resolve_id(self, acting: object, kind: str, ref: str) -> str:
        self.calls.append((kind, ref))
        answer = self.answers[ref]
        if isinstance(answer, Exception):
            raise answer
        return str(answer)

    async def get_group(self, acting: object, group_id: str) -> dict[str, Any]:
        raise AssertionError("an unresolved id never reaches gate")


async def test_resolve_ref_keeps_full_ids_and_resolves_short_ones() -> None:
    gate = ResolveGate({"df79307a": FULL})
    client = cast(GateClient, gate)
    assert await resolve_ref(client, 1, "group", FULL.upper()) == FULL
    assert gate.calls == []  # a full id needs no lookup
    assert await resolve_ref(client, 1, "group", " DF79307A ") == FULL
    assert gate.calls == [("group", "df79307a")]
    assert await resolve_refs(client, 1, "group", f"df79307a, {FULL}") == [FULL, FULL]


async def test_ambiguous_short_id_gives_a_clear_card() -> None:
    ambiguous = GateError(409, "TWR-GEN-005", "Several items start with this identifier")
    gate = ResolveGate({"df7930": ambiguous})
    reply = await fc.group_show(cast(GateClient, gate), 1, "en", "df7930")
    assert "type more characters" in reply.text and "TWR-GEN-005" in reply.text


async def test_gate_resolves_a_prefix(
    client: httpx.AsyncClient, make_token: TokenFactory, db: Database
) -> None:
    built = await build_draft(db)
    headers = {"Authorization": f"Bearer {await make_token('intercom')}"}
    short = str(built.case)[:8]
    r = await client.get(f"/v1/ids/case/{short}", headers=headers)
    assert r.status_code == 200 and r.json() == {"kind": "case", "id": str(built.case)}
    r = await client.get(f"/v1/ids/draft/{str(built.verdict)[:6]}", headers=headers)
    assert r.status_code == 200 and r.json()["id"] == str(built.verdict)
    missing = await client.get("/v1/ids/narrative/ffffffff", headers=headers)
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "TWR-GEN-002"
    bad = await client.get("/v1/ids/group/abc", headers=headers)
    assert bad.status_code == 422
    wrong_kind = await client.get(f"/v1/ids/user/{short}", headers=headers)
    assert wrong_kind.status_code == 422
    assert (await client.get(f"/v1/ids/case/{short}")).status_code in (401, 403)


def test_parse_ref() -> None:
    assert parse_ref(" FBAA8363 ") == "fbaa8363"
    assert parse_ref("fbaa83631") == "fbaa83631"
    assert parse_ref(FULL.upper()) == uuid.UUID(FULL)
    for bad in ("fbaa836", "xyz12345", "", "0" * 33):
        with pytest.raises(InvalidInput):
            parse_ref(bad)


async def test_case_short_id_is_stable_and_used_by_every_route(
    client: httpx.AsyncClient, make_token: TokenFactory, db: Database
) -> None:
    """The trigger gives 8 characters, one more on a collision, and never changes it: a
    link already posted keeps naming the same case when a later one shares its prefix."""
    first_id = uuid.UUID("fbaa8363-1f02-4e51-9e4d-5cb40e3283dc")
    clash_id = uuid.UUID("fbaa8363-9999-4e51-9e4d-5cb40e3283dc")
    async with db.transaction() as s:
        for pid in (first_id, clash_id):
            s.add(Case(public_id=pid, kind="fact_check", status="published", opened_by="t"))
    async with db.transaction() as s:
        rows = {p: sid for p, sid in await s.execute(select(Case.public_id, Case.short_id))}
        await s.execute(update(Case).where(Case.public_id == first_id).values(short_id="00000000"))
    assert rows[first_id] == "fbaa8363" and rows[clash_id] == "fbaa83639"

    public = await client.get("/v1/cases/FBAA8363")
    assert public.status_code == 200, public.text
    assert public.json()["short_id"] == "fbaa8363"  # unchanged by the update above
    assert public.json()["public_id"] == str(first_id)
    assert (await client.get("/v1/cases/fbaa83639")).json()["public_id"] == str(clash_id)
    assert (await client.get(f"/v1/cases/{first_id}")).json()["short_id"] == "fbaa8363"
    details = await client.get("/v1/cases/fbaa8363/details")
    assert details.status_code == 200 and details.json()["short_id"] == "fbaa8363"
    missing = await client.get("/v1/cases/fbaa8364")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "TWR-DOS-001"
    assert (await client.get("/v1/cases/fbaa83")).status_code == 422

    headers = {"Authorization": f"Bearer {await make_token('intercom')}"}
    resolved = await client.get("/v1/ids/case/fbaa8363", headers=headers)
    assert resolved.json() == {"kind": "case", "id": str(first_id)}  # exact short id wins
    team = await client.get("/v1/cases/fbaa83639", headers=headers)
    assert team.json()["short_id"] == "fbaa83639"
