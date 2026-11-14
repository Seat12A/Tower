"""Gate endpoints of groups, reference facts and bands; enriched draft view; public read of
inherited verdicts (identical signature and band)."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select, update

from tower.core.db.database import Database
from tower.core.models.factcheck import (
    Claim,
    ClaimGroup,
    ClaimGroupMember,
    ContentClaim,
    ContextNote,
    Investigation,
    Verdict,
)
from tower.core.models.factcheck_v1 import ClaimGroupEvent
from tower.core.models.ingestion import ContentItem

from .conftest import TokenFactory
from .test_drafts_usecases import Built, build_draft
from .test_factcheck_references import (
    active_draft,
    new_fact,
    prepare_group,
)

REVIEWER = 1001
EDITOR = 1002
ADMIN = 1003
AddStaff = Callable[..., Coroutine[Any, Any, None]]
UNKNOWN = "11111111-2222-3333-4444-555555555555"
EXT_A = "1850000000000000201"
EXT_B = "1850000000000000202"
EXT_C = "1850000000000000203"


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
    await add_staff(ADMIN, ["admin"])
    return token


async def group_public(db: Database, built: Built) -> str:
    async with db.session() as s:
        group = await s.get(ClaimGroup, built.group_id)
        assert group is not None
        return str(group.public_id)


async def claim_public(db: Database, claim_id: int) -> str:
    async with db.session() as s:
        claim = await s.get(Claim, claim_id)
        assert claim is not None
        return str(claim.public_id)


async def add_post(
    db: Database, group_id: int, platform: str, ext: str, *, text: str = "Le chômage a doublé"
) -> tuple[int, int]:
    """Another post saying the same thing, joined to the group: ``(claim id, content id)``."""
    async with db.transaction() as s:
        content = ContentItem(
            platform=platform,
            external_id=ext,
            text=text,
            lang="fr",
            lang_confidence=0.99,
            useful_words=6,
            content_hash=bytes(32),
            simhash=sum(ord(c) for c in ext) % 1000,
            created_at_platform=datetime(2026, 5, 1, tzinfo=UTC),
        )
        s.add(content)
        claim = Claim(
            lang_original="fr",
            text_original=text,
            text_fr=text,
            claim_type="statistic",
            verifiability=0.9,
            prompt_version="1",
        )
        s.add(claim)
        await s.flush()
        s.add(ClaimGroupMember(group_id=group_id, claim_id=claim.id))
        s.add(ContentClaim(content_id=content.id, claim_id=claim.id, position=0))
        return claim.id, content.id


# --- groups -------------------------------------------------------------------------


async def test_group_detail_and_members(client: httpx.AsyncClient, bot: str, db: Database) -> None:
    built = await build_draft(db, ext=EXT_A)
    group = await group_public(db, built)
    moved, _ = await add_post(db, built.group_id, "bluesky", "bsky-1")
    r = await client.get(f"/v1/groups/{group}", headers=h(bot, REVIEWER))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == group and body["level"] == "N3" and body["merged_into"] is None
    assert body["active_verdict"] is None and body["reference_fact"] is None
    assert len(body["members"]) == 2
    first = body["members"][0]
    assert first["signature"]["claim_type"] == "statistic" and first["representative"] is True
    assert first["contents"][0]["url"] == f"https://x.com/i/status/{EXT_A}"
    assert body["members"][1]["contents"][0]["platform"] == "bluesky"
    assert body["members"][1]["contents"][0]["url"] is None
    assert body["journal"] == []

    members = await client.get(f"/v1/groups/{group}/members", headers=h(bot, REVIEWER))
    assert members.status_code == 200 and len(members.json()) == 2
    # Split one claim off: the removed member shows only with include_removed.
    r = await client.post(
        f"/v1/groups/{group}/split",
        json={"claim_ids": [await claim_public(db, moved)], "reason": "different story"},
        headers=h(bot, EDITOR),
    )
    assert r.status_code == 200, r.text
    new_group = r.json()
    assert new_group["id"] != group and len(new_group["members"]) == 1
    assert new_group["state"] == "queued" and new_group["active_verdict"] is None
    assert new_group["journal"][0]["kind"] == "split"
    assert new_group["journal"][0]["claim_ids"] == [await claim_public(db, moved)]
    assert all(not isinstance(v, int) for v in new_group["journal"][0]["data"].values())
    again = await client.get(f"/v1/groups/{group}/members", headers=h(bot, REVIEWER))
    assert len(again.json()) == 1
    everything = await client.get(
        f"/v1/groups/{group}/members?include_removed=true", headers=h(bot, REVIEWER)
    )
    assert len(everything.json()) == 2
    assert any(m["removed_at"] and m["removed_reason"] for m in everything.json())


async def test_group_endpoint_errors(client: httpx.AsyncClient, bot: str, db: Database) -> None:
    r = await client.get(f"/v1/groups/{UNKNOWN}", headers=h(bot, REVIEWER))
    assert r.status_code == 404 and r.json()["error"]["code"] == "TWR-FCK-020"
    assert (
        await client.get(f"/v1/groups/{UNKNOWN}/members", headers=h(bot, EDITOR))
    ).status_code == 404
    assert (await client.get(f"/v1/groups/{UNKNOWN}")).status_code in (401, 403)
    built = await build_draft(db, ext=EXT_A)
    group = await group_public(db, built)
    for path, body in (
        (f"/v1/groups/{group}/merge", {"source_group_id": UNKNOWN, "reason": "x"}),
        (f"/v1/groups/{group}/split", {"claim_ids": [UNKNOWN], "reason": "x"}),
    ):
        assert (await client.post(path, json=body, headers=h(bot, REVIEWER))).status_code == 403
    r = await client.post(
        f"/v1/groups/{group}/merge",
        json={"source_group_id": UNKNOWN, "reason": "x"},
        headers=h(bot, EDITOR),
    )
    assert r.status_code == 404 and r.json()["error"]["code"] == "TWR-FCK-020"
    r = await client.post(
        f"/v1/groups/{group}/split",
        json={"claim_ids": [UNKNOWN], "reason": "x"},
        headers=h(bot, EDITOR),
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "TWR-FCK-024"
    r = await client.post(
        f"/v1/groups/{group}/split", json={"claim_ids": [], "reason": "x"}, headers=h(bot, EDITOR)
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "TWR-FCK-024"


async def test_merge_nominal_and_conflicts(
    client: httpx.AsyncClient, bot: str, db: Database
) -> None:
    a = await build_draft(db, ext=EXT_A)
    b = await build_draft(db, ext=EXT_B)
    ga, gb = await group_public(db, a), await group_public(db, b)
    r = await client.post(
        f"/v1/groups/{ga}/merge",
        json={"source_group_id": gb, "reason": "same"},
        headers=h(bot, EDITOR),
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["members"]) == 2 and r.json()["journal"][0]["kind"] == "merged"
    source = (await client.get(f"/v1/groups/{gb}", headers=h(bot, EDITOR))).json()
    assert source["state"] == "merged" and source["merged_into"] == ga
    again = await client.post(
        f"/v1/groups/{ga}/merge",
        json={"source_group_id": gb, "reason": "x"},
        headers=h(bot, EDITOR),
    )
    assert again.status_code == 409 and again.json()["error"]["code"] == "TWR-FCK-021"


async def test_merge_verdict_rules(client: httpx.AsyncClient, bot: str, db: Database) -> None:
    plain = await build_draft(db, ext=EXT_A)
    holder = await build_draft(db, ext=EXT_B, previous_active=True)
    other = await build_draft(db, ext=EXT_C, previous_active=True)
    async with db.transaction() as s:
        group = await s.get(ClaimGroup, other.group_id)
        assert group is not None and group.active_verdict_id is not None
        await s.execute(
            update(Verdict).where(Verdict.id == group.active_verdict_id).values(veracity="false")
        )
    gp = await group_public(db, plain)
    gh = await group_public(db, holder)
    go = await group_public(db, other)
    r = await client.post(
        f"/v1/groups/{gp}/merge",
        json={"source_group_id": gh, "reason": "x"},
        headers=h(bot, EDITOR),
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "TWR-FCK-023"
    r = await client.post(
        f"/v1/groups/{gh}/merge",
        json={"source_group_id": go, "reason": "x"},
        headers=h(bot, EDITOR),
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "TWR-FCK-022"
    detail = (await client.get(f"/v1/groups/{gh}", headers=h(bot, EDITOR))).json()
    assert detail["active_verdict"]["veracity"] == "mostly_true"
    assert detail["active_verdict"]["status"] == "active"


# --- reference facts ----------------------------------------------------------------

FACT = {
    "indicator": "fr.unemployment_rate",
    "label": "Taux de chômage (BIT), France",
    "value": "7.3",
    "unit": "%",
    "period_start": "2026-01-01",
    "period_end": "2026-03-31",
    "territory": "FR",
    "source_url": "https://www.insee.fr/serie/001688527",
    "source_name": "INSEE",
    "published_at": "2026-05-15",
    "aliases": ["Taux de chômage"],
}


async def test_fact_lifecycle(client: httpx.AsyncClient, bot: str) -> None:
    r = await client.post("/v1/reference-facts", json=FACT, headers=h(bot, REVIEWER))
    assert r.status_code == 201, r.text
    fact = r.json()
    assert fact["status"] == "pending" and fact["origin"] == "manual" and fact["unit"] == "percent"
    assert fact["value"] == "7.3" and fact["created_by"].startswith("staff:")

    # Reviewers write facts but only editors validate them.
    r = await client.post(f"/v1/reference-facts/{fact['id']}/validate", headers=h(bot, REVIEWER))
    assert r.status_code == 403
    r = await client.post(f"/v1/reference-facts/{fact['id']}/validate", headers=h(bot, EDITOR))
    assert r.status_code == 200 and r.json()["status"] == "validated"
    assert r.json()["validated_by"].startswith("staff:")

    got = await client.get(f"/v1/reference-facts/{fact['id']}", headers=h(bot, REVIEWER))
    assert got.status_code == 200 and got.json()["id"] == fact["id"]
    listed = await client.get(
        "/v1/reference-facts?status=validated&indicator=fr.unemployment_rate",
        headers=h(bot, REVIEWER),
    )
    assert [f["id"] for f in listed.json()] == [fact["id"]]
    none = await client.get("/v1/reference-facts?status=pending", headers=h(bot, REVIEWER))
    assert none.json() == []

    r = await client.post("/v1/reference-facts", json=FACT, headers=h(bot, EDITOR))
    second = r.json()["id"]
    r = await client.post(
        f"/v1/reference-facts/{second}/reject",
        json={"reason": "wrong series"},
        headers=h(bot, REVIEWER),
    )
    assert r.status_code == 403
    r = await client.post(
        f"/v1/reference-facts/{second}/reject",
        json={"reason": "wrong series"},
        headers=h(bot, EDITOR),
    )
    assert r.status_code == 200 and r.json()["status"] == "rejected"


async def test_fact_errors(client: httpx.AsyncClient, bot: str) -> None:
    r = await client.get(f"/v1/reference-facts/{UNKNOWN}", headers=h(bot, EDITOR))
    assert r.status_code == 404 and r.json()["error"]["code"] == "TWR-FCK-025"
    r = await client.get(f"/v1/reference-facts/{UNKNOWN}/bands", headers=h(bot, EDITOR))
    assert r.status_code == 404 and r.json()["error"]["code"] == "TWR-FCK-025"
    r = await client.post(f"/v1/reference-facts/{UNKNOWN}/validate", headers=h(bot, EDITOR))
    assert r.status_code == 404 and r.json()["error"]["code"] == "TWR-FCK-025"
    r = await client.post(
        "/v1/reference-facts", json={**FACT, "indicator": "Not A Key"}, headers=h(bot, EDITOR)
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "TWR-FCK-026"
    r = await client.post(
        "/v1/reference-facts", json={**FACT, "territory": "nowhere!"}, headers=h(bot, EDITOR)
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "TWR-FCK-026"
    assert (await client.get("/v1/reference-facts")).status_code in (401, 403)
    r = await client.post("/v1/reference-facts", json=FACT)
    assert r.status_code in (401, 403)


async def test_fact_from_verdict_and_bands(
    client: httpx.AsyncClient, bot: str, db: Database
) -> None:
    built = await active_draft(db, EXT_A)
    body = {
        "verdict_id": str(built.verdict),
        "value": "7.3",
        "period_start": "2026-01-01",
        "period_end": "2026-03-31",
        "source_url": "https://www.insee.fr/x",
        "source_name": "INSEE",
        "published_at": "2026-05-15",
    }
    r = await client.post("/v1/reference-facts/from-verdict", json=body, headers=h(bot, EDITOR))
    assert r.status_code == 201, r.text
    fact = r.json()
    assert fact["origin"] == "investigation" and fact["unit"] == "percent"
    # Not validated yet: no band can be validated.
    r = await client.post(
        "/v1/verdict-bands/from-verdict",
        json={"verdict_id": str(built.verdict)},
        headers=h(bot, EDITOR),
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "TWR-FCK-028"
    r = await client.post(f"/v1/reference-facts/{fact['id']}/validate", headers=h(bot, EDITOR))
    assert r.status_code == 200
    group = (
        await client.get(f"/v1/groups/{await group_public(db, built)}", headers=h(bot, EDITOR))
    ).json()
    assert group["band"] == "within_3_tau" and group["reference_fact"]["id"] == fact["id"]

    r = await client.post(
        "/v1/verdict-bands/from-verdict",
        json={"verdict_id": str(built.verdict)},
        headers=h(bot, REVIEWER),
    )
    assert r.status_code == 403
    r = await client.post(
        "/v1/verdict-bands/from-verdict",
        json={"verdict_id": str(built.verdict)},
        headers=h(bot, EDITOR),
    )
    assert r.status_code == 201, r.text
    band = r.json()
    assert band["band"] == "within_3_tau" and band["status"] == "active"
    assert band["fact_id"] == fact["id"] and band["source_verdict_id"] == str(built.verdict)

    bands = await client.get(f"/v1/reference-facts/{fact['id']}/bands", headers=h(bot, REVIEWER))
    assert [b["id"] for b in bands.json()] == [band["id"]]
    r = await client.post(
        f"/v1/verdict-bands/{band['id']}/revoke", json={"reason": "wrong"}, headers=h(bot, EDITOR)
    )
    assert r.status_code == 200 and r.json()["status"] == "revoked"
    r = await client.post(
        f"/v1/verdict-bands/{band['id']}/revoke", json={"reason": "wrong"}, headers=h(bot, EDITOR)
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "TWR-FCK-028"

    # Explicit band creation.
    explicit = {
        "fact_id": fact["id"],
        "band": "beyond",
        "side": "above",
        "veracity": "false",
        "context_need": "useful",
        "context_types": [],
        "source_verdict_id": str(built.verdict),
    }
    r = await client.post("/v1/verdict-bands", json=explicit, headers=h(bot, EDITOR))
    assert r.status_code == 201 and r.json()["label"] == "beyond 3 tau of the fact (above)"
    r = await client.post("/v1/verdict-bands", json=explicit, headers=h(bot, REVIEWER))
    assert r.status_code == 403
    r = await client.post(
        f"/v1/verdict-bands/{UNKNOWN}/revoke", json={"reason": "x"}, headers=h(bot, EDITOR)
    )
    assert r.status_code == 404


async def test_review_accepts_validate_band(
    client: httpx.AsyncClient, bot: str, db: Database
) -> None:
    fact = await new_fact(db)
    built = await build_draft(db, ext=EXT_B)
    await prepare_group(db, built, fact=fact)
    async with db.transaction() as s:
        group = await s.get(ClaimGroup, built.group_id)
        assert group is not None
        group.band, group.band_side = "within_3_tau", "above"
    r = await client.post(
        f"/v1/drafts/{built.verdict}/review",
        json={"decision": "accept", "validate_band": True},
        headers=h(bot, EDITOR),
    )
    assert r.status_code == 200, r.text
    bands = await client.get(f"/v1/reference-facts/{fact.public_id}/bands", headers=h(bot, EDITOR))
    assert len(bands.json()) == 1 and bands.json()[0]["status"] == "active"


# --- draft view ---------------------------------------------------------------------


async def test_draft_view_group_fields(client: httpx.AsyncClient, bot: str, db: Database) -> None:
    built = await build_draft(db, ext=EXT_A)
    await add_post(db, built.group_id, "x", EXT_B)
    await add_post(db, built.group_id, "bluesky", "bsky-9")
    r = await client.get(f"/v1/drafts/{built.verdict}", headers=h(bot, REVIEWER))
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["group"] == {
        "id": await group_public(db, built),
        "level": "N3",
        "members": 3,
    }
    assert view["platforms"] == ["bluesky", "x"]
    assert {(p["platform"], p["external_id"]) for p in view["other_posts"]} == {
        ("x", EXT_B),
        ("bluesky", "bsky-9"),
    }
    posts = {p["external_id"]: p for p in view["other_posts"]}
    assert posts[EXT_B]["url"] == f"https://x.com/i/status/{EXT_B}"
    assert posts[EXT_B]["posted_at"].startswith("2026-05-01")
    assert view["reusable_verdict"] is None and view["reference_fact"] is None
    assert view["band"] is None and view["outdated"] is False
    assert view["external_factchecks"] == []
    listed = await client.get("/v1/drafts", headers=h(bot, REVIEWER))
    assert listed.json()[0]["group"]["members"] == 3


async def test_draft_view_fact_band_outdated_and_external(
    client: httpx.AsyncClient, bot: str, db: Database
) -> None:
    fact = await new_fact(db)
    built = await build_draft(db, ext=EXT_A)
    await prepare_group(db, built, fact=fact)
    external = [
        {"url": "https://factcheck.example/a", "publisher": "Ex", "rating": "False", "title": "t"}
    ]
    async with db.transaction() as s:
        group = await s.get(ClaimGroup, built.group_id)
        assert group is not None
        group.band, group.band_side = "within_1_tau", "above"
        await s.execute(
            update(Investigation)
            .where(Investigation.claim_group_id == built.group_id)
            .values(flags={"outdated": True, "external_factchecks": external})
        )
    view = (await client.get(f"/v1/drafts/{built.verdict}", headers=h(bot, EDITOR))).json()
    assert view["reference_fact"] == {
        "indicator": "fr.unemployment_rate",
        "label": "Taux de chômage (BIT), France",
        "value": "7.3",
        "unit": "percent",
        "period_start": "2026-01-01",
        "period_end": "2026-03-31",
        "territory": "FR",
        "status": "validated",
    }
    assert view["band"] == {
        "band": "within_1_tau",
        "side": "above",
        "label": "within 1 tau of the fact (above)",
    }
    assert view["outdated"] is True and view["external_factchecks"] == external


async def test_draft_view_reusable_verdict_suggestion(
    client: httpx.AsyncClient, bot: str, db: Database
) -> None:
    built = await build_draft(db, ext=EXT_A)
    other = await build_draft(db, ext=EXT_B, previous_active=True)
    async with db.transaction() as s:
        s.add(
            ClaimGroupEvent(
                group_id=built.group_id,
                kind="created",
                other_group_id=other.group_id,
                actor="system:factcheck",
                data={"rule": "new:related", "related_group_id": other.group_id},
            )
        )
    view = (await client.get(f"/v1/drafts/{built.verdict}", headers=h(bot, EDITOR))).json()
    assert view["reusable_verdict"]["group_id"] == await group_public(db, other)
    assert view["reusable_verdict"]["veracity"] == "mostly_true"
    # No suggestion when the related group has no active verdict.
    plain = await build_draft(db, ext=EXT_C)
    async with db.transaction() as s:
        await s.execute(
            update(ClaimGroupEvent)
            .where(ClaimGroupEvent.other_group_id == other.group_id)
            .values(other_group_id=plain.group_id)
        )
    view = (await client.get(f"/v1/drafts/{built.verdict}", headers=h(bot, EDITOR))).json()
    assert view["reusable_verdict"] is None


# --- public read of inherited verdicts ----------------------------------------------


async def test_public_read_of_a_joined_variant(
    client: httpx.AsyncClient, bot: str, db: Database
) -> None:
    built = await build_draft(db, ext=EXT_A)
    r = await client.post(
        f"/v1/drafts/{built.verdict}/review", json={"decision": "accept"}, headers=h(bot, EDITOR)
    )
    assert r.status_code == 200
    _, joined = await add_post(db, built.group_id, "x", EXT_B)
    assert joined
    origin = (await client.get(f"/v1/content/x/{EXT_A}/verdict")).json()["claims"][0]
    assert origin["inherited"] is None and origin["note"] is not None
    variant = (await client.get(f"/v1/content/x/{EXT_B}/verdict")).json()["claims"][0]
    assert variant["veracity"] == origin["veracity"]
    assert variant["inherited"] == {
        "rule": "identical_signature",
        "origin_verdict_id": str(built.verdict),
        "origin_url": f"https://x.com/i/status/{EXT_A}",
    }
    # Formulation note of the SAME group: shown to its own readers (identical signature).
    assert variant["note"] is not None
    # A provisional note is never inherited, even by an identical-signature variant.
    async with db.transaction() as s:
        note = await s.get(ContextNote, built.note_id)
        assert note is not None
        note.regime = "provisional"
        note.expires_at = datetime.now(UTC) + timedelta(hours=6)
    variant = (await client.get(f"/v1/content/x/{EXT_B}/verdict")).json()["claims"][0]
    assert variant["note"] is None and variant["veracity"] == origin["veracity"]
    origin_again = (await client.get(f"/v1/content/x/{EXT_A}/verdict")).json()["claims"][0]
    assert origin_again["note"] is not None  # the investigated post keeps its own note


async def band_variant(db: Database) -> tuple[Built, Built]:
    """An active origin verdict and a second group covered through a band of it."""
    origin = await active_draft(db, EXT_A)
    variant = await build_draft(db, ext=EXT_B)
    async with db.transaction() as s:
        inherited = Verdict(
            claim_group_id=variant.group_id,
            veracity="false",
            context_need="none",
            status="active",
            reused_from_verdict_id=origin.verdict_id,
            reuse_rule="band",
            validated_by="system:factcheck",
            validated_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        s.add(inherited)
        await s.flush()
        group = await s.get(ClaimGroup, variant.group_id)
        assert group is not None
        group.active_verdict_id = inherited.id
        group.state = "passive"
        # The variant's own draft is not served: only the inherited verdict is.
        await s.execute(
            update(Verdict).where(Verdict.id == variant.verdict_id).values(status="rejected")
        )
    return origin, variant


async def test_public_read_of_a_band_inherited_variant(
    client: httpx.AsyncClient, db: Database
) -> None:
    origin, _ = await band_variant(db)
    url = f"/v1/content/x/{EXT_B}/verdict"
    claim = (await client.get(url)).json()["claims"][0]
    assert claim["veracity"] == "false"
    assert claim["inherited"] == {
        "rule": "band",
        "origin_verdict_id": str(origin.verdict),
        "origin_url": f"https://x.com/i/status/{EXT_A}",
    }
    # The origin note is a formulation note: never shown across groups.
    assert claim["note"] is None
    # An object-regime note is inheritable.
    async with db.transaction() as s:
        note = await s.get(ContextNote, origin.note_id)
        assert note is not None
        note.regime = "object"
    claim = (await client.get(url)).json()["claims"][0]
    assert claim["note"] is not None and claim["note"]["lang"] == "fr"
    # Provisional or expired notes are hidden.
    async with db.transaction() as s:
        note = await s.get(ContextNote, origin.note_id)
        assert note is not None
        note.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    assert (await client.get(url)).json()["claims"][0]["note"] is None
    async with db.transaction() as s:
        note = await s.get(ContextNote, origin.note_id)
        assert note is not None
        note.expires_at = None
        note.regime = "provisional"
    assert (await client.get(url)).json()["claims"][0]["note"] is None


async def test_public_read_never_shows_an_expired_verdict(
    client: httpx.AsyncClient, db: Database
) -> None:
    await band_variant(db)
    url = f"/v1/content/x/{EXT_B}/verdict"
    assert (await client.get(url)).status_code == 200
    async with db.transaction() as s:
        await s.execute(
            update(Verdict)
            .where(Verdict.reuse_rule == "band")
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    r = await client.get(url)
    assert r.status_code == 404 and r.json()["error"]["code"] == "TWR-FCK-013"
    # The origin verdict (no expiry) is still served.
    assert (await client.get(f"/v1/content/x/{EXT_A}/verdict")).status_code == 200
    async with db.session() as s:
        assert (await s.scalars(select(Verdict.id))).all()


async def test_openapi_tags(client: httpx.AsyncClient) -> None:
    spec = (await client.get("/openapi.json")).json()
    tags = {
        path: {t for op in item.values() for t in op["tags"]}
        for path, item in spec["paths"].items()
    }
    assert tags["/v1/groups/{group_id}"] == {"groups"}
    assert tags["/v1/reference-facts"] == {"references"}
    assert tags["/v1/verdict-bands"] == {"references"}
    assert uuid.UUID(UNKNOWN)
