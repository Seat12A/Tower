"""Gate endpoints of narratives (list, detail, merge, split, title) and the narrative and
coordination fields of drafts and groups."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from tower.core.db.database import Database

from .conftest import TokenFactory
from .test_drafts_usecases import build_draft
from .test_factcheck_coordination import NOW, authors, post
from .test_factcheck_matching import _reset  # noqa: F401  (autouse fixture)
from .test_factcheck_narratives import build_narrative, narrative_public, public_id, seed
from .test_factcheck_narratives import unit as axis
from .test_gate_groups_references import group_public

REVIEWER = 1001
EDITOR = 1002
AddStaff = Callable[..., Coroutine[Any, Any, None]]
UNKNOWN = "11111111-2222-3333-4444-555555555555"


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


async def run(db: Database, sql: str, **params: Any) -> None:
    async with db.transaction() as s:
        await s.execute(text(sql), params)


async def two_group_narrative(db: Database, base: int) -> tuple[int, list[int]]:
    gids = [await seed(db, f"n{base}-{i}", axis(base + i), reach=10 * (i + 1)) for i in range(3)]
    return await build_narrative(db, *gids), gids


async def add_stats(db: Database, nid: int, span: str, count: int) -> None:
    async with db.transaction() as s:
        for i in range(count):
            await s.execute(
                text(
                    "INSERT INTO narrative_stats (narrative_id, span, window_end, posts, "
                    "flow_posts, share, accounts, platforms, reach, score) "
                    "VALUES (:n, :s, :t, :i, 1000, :sh, 40, 2, 5, 1.5)"
                ),
                {
                    "n": nid,
                    "s": span,
                    "t": NOW - timedelta(hours=count - i),
                    "i": i,
                    "sh": 0.01 * i,
                },
            )


# --- list -----------------------------------------------------------------------------


async def test_list_shows_open_narratives_with_their_last_hour(
    client: httpx.AsyncClient, bot: str, db: Database
) -> None:
    nid, _ = await two_group_narrative(db, 400)
    await run(
        db,
        "UPDATE narratives SET title = 'Story A', title_status = 'draft', "
        "theme = 'health' WHERE id = :n",
        n=nid,
    )
    await add_stats(db, nid, "1h", 3)
    closed, _ = await two_group_narrative(db, 410)
    await run(db, "UPDATE narratives SET state = 'closed' WHERE id = :n", n=closed)
    r = await client.get("/v1/narratives", headers=h(bot, REVIEWER))
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [row["id"] for row in rows] == [str(await narrative_public(db, nid))]
    row = rows[0]
    assert row["title"] == "Story A" and row["title_status"] == "draft"
    assert row["state"] == "active" and row["theme"] == "health" and row["groups"] == 3
    assert row["stats_1h"]["accounts"] == 40 and row["stats_1h"]["platforms"] == 2
    assert row["stats_1h"]["share"] == pytest.approx(0.02)  # the latest window
    assert row["opened_at"] and row["last_activity_at"]


async def test_list_filters_by_state_and_validates_it(
    client: httpx.AsyncClient, bot: str, db: Database
) -> None:
    a, _ = await two_group_narrative(db, 420)
    b, _ = await two_group_narrative(db, 430)
    await run(db, "UPDATE narratives SET state = 'surge' WHERE id = :n", n=b)
    surge = await client.get("/v1/narratives?state=surge", headers=h(bot, REVIEWER))
    assert [r["id"] for r in surge.json()] == [str(await narrative_public(db, b))]
    all_open = await client.get("/v1/narratives?limit=1", headers=h(bot, REVIEWER))
    assert len(all_open.json()) == 1
    bad = await client.get("/v1/narratives?state=nope", headers=h(bot, REVIEWER))
    assert bad.status_code == 422
    assert (await client.get("/v1/narratives")).status_code in (401, 403)
    assert a != b


# --- detail -----------------------------------------------------------------------------


async def test_detail_carries_groups_stats_journal_and_novelty(
    client: httpx.AsyncClient, bot: str, db: Database
) -> None:
    previous, _ = await two_group_narrative(db, 440)
    nid, gids = await two_group_narrative(db, 450)
    await run(
        db,
        "UPDATE narratives SET previous_narrative_id = :p, split_suggested = true, "
        "representative_group_id = :g WHERE id = :n",
        p=previous,
        g=gids[1],
        n=nid,
    )
    await run(db, "UPDATE claim_groups SET light_signal = 'likely_false' WHERE id = :g", g=gids[2])
    for span in ("5m", "1h", "6h"):
        await add_stats(db, nid, span, 15)
    async with db.transaction() as s:
        for i in range(12):
            await s.execute(
                text(
                    "INSERT INTO narrative_novelty (narrative_id, fingerprint, kind, posts, "
                    "accounts, forced_at) VALUES (:n, :f, 'number', 5, 4, :t)"
                ),
                {"n": nid, "f": f"fp{i}", "t": NOW - timedelta(minutes=i) if i < 11 else None},
            )
    r = await client.get(
        f"/v1/narratives/{await narrative_public(db, nid)}", headers=h(bot, REVIEWER)
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "active" and body["split_suggested"] is True
    assert body["previous_narrative"] == str(await narrative_public(db, previous))
    assert body["representative_group"] == str(await public_id(db, gids[1]))
    assert body["medoids"][0] == str(await public_id(db, gids[0]))  # the founder first
    assert {g["id"] for g in body["groups"]} == {str(await public_id(db, g)) for g in gids}
    assert [g["reach"] for g in body["groups"]] == [30, 20, 10]  # by reach
    assert {g["light_signal"] for g in body["groups"]} == {None, "likely_false"}
    assert all(g["verdict"] is None and g["level"] == "N1" for g in body["groups"])
    for span in ("5m", "1h", "6h"):
        points = body["stats"][span]
        assert len(points) == 12
        assert points == sorted(points, key=lambda p: p["window_end"])  # oldest first
        assert points[-1]["posts"] == 14
    assert [e["kind"] for e in body["journal"]] == [
        "attached",
        "attached",
        "created",
    ]  # newest first
    assert body["journal"][0]["actor"]
    assert len(body["novelty"]) == 10  # only those that took, capped
    assert all(n["fingerprint"] != "fp11" for n in body["novelty"])
    assert body["stats_1h"]["accounts"] == 40


async def test_detail_of_an_unknown_narrative_is_043(client: httpx.AsyncClient, bot: str) -> None:
    r = await client.get(f"/v1/narratives/{UNKNOWN}", headers=h(bot, REVIEWER))
    assert r.status_code == 404 and r.json()["error"]["code"] == "TWR-FCK-043"


# --- merge, split, title ----------------------------------------------------------------


async def test_merge_needs_manage_and_reports_errors(
    client: httpx.AsyncClient, bot: str, db: Database
) -> None:
    a, _ = await two_group_narrative(db, 460)
    b, _ = await two_group_narrative(db, 470)
    target, source = str(await narrative_public(db, a)), str(await narrative_public(db, b))
    body = {"source_id": source, "reason": "same story"}
    denied = await client.post(
        f"/v1/narratives/{target}/merge", json=body, headers=h(bot, REVIEWER)
    )
    assert denied.status_code == 403
    unknown = await client.post(
        f"/v1/narratives/{target}/merge",
        json={"source_id": UNKNOWN, "reason": "x"},
        headers=h(bot, EDITOR),
    )
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "TWR-FCK-043"
    same = await client.post(
        f"/v1/narratives/{target}/merge",
        json={"source_id": target, "reason": "x"},
        headers=h(bot, EDITOR),
    )
    assert same.status_code == 422 and same.json()["error"]["code"] == "TWR-FCK-044"
    ok = await client.post(f"/v1/narratives/{target}/merge", json=body, headers=h(bot, EDITOR))
    assert ok.status_code == 200, ok.text
    assert ok.json()["id"] == target and len(ok.json()["groups"]) == 6
    assert ok.json()["journal"][0]["kind"] == "merged"
    closed = (await client.get(f"/v1/narratives/{source}", headers=h(bot, EDITOR))).json()
    assert closed["state"] == "closed" and closed["merged_into"] == target
    again = await client.post(f"/v1/narratives/{target}/merge", json=body, headers=h(bot, EDITOR))
    assert again.status_code == 422 and again.json()["error"]["code"] == "TWR-FCK-044"


async def test_split_moves_groups_into_a_new_narrative(
    client: httpx.AsyncClient, bot: str, db: Database
) -> None:
    nid, gids = await two_group_narrative(db, 480)
    public = str(await narrative_public(db, nid))
    moved = [str(await public_id(db, gids[2]))]
    body = {"group_ids": moved, "reason": "different story"}
    denied = await client.post(
        f"/v1/narratives/{public}/split", json=body, headers=h(bot, REVIEWER)
    )
    assert denied.status_code == 403
    founder = await client.post(
        f"/v1/narratives/{public}/split",
        json={"group_ids": [str(await public_id(db, gids[0]))], "reason": "x"},
        headers=h(bot, EDITOR),
    )
    assert founder.status_code == 422 and founder.json()["error"]["code"] == "TWR-FCK-044"
    missing = await client.post(
        f"/v1/narratives/{UNKNOWN}/split", json=body, headers=h(bot, EDITOR)
    )
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "TWR-FCK-043"
    ok = await client.post(f"/v1/narratives/{public}/split", json=body, headers=h(bot, EDITOR))
    assert ok.status_code == 200, ok.text
    created = ok.json()
    assert created["id"] != public and [g["id"] for g in created["groups"]] == moved
    assert created["journal"][0]["kind"] == "split"
    old = (await client.get(f"/v1/narratives/{public}", headers=h(bot, EDITOR))).json()
    assert len(old["groups"]) == 2


async def test_title_is_validated_or_corrected_by_a_reviewer(
    client: httpx.AsyncClient, bot: str, db: Database
) -> None:
    nid, _ = await two_group_narrative(db, 490)
    public = str(await narrative_public(db, nid))
    empty = await client.post(f"/v1/narratives/{public}/title", json={}, headers=h(bot, REVIEWER))
    assert empty.status_code == 422 and empty.json()["error"]["code"] == "TWR-FCK-044"
    await run(
        db,
        "UPDATE narratives SET title = 'Draft title', title_status = 'draft' WHERE id = :n",
        n=nid,
    )
    done = await client.post(f"/v1/narratives/{public}/title", json={}, headers=h(bot, REVIEWER))
    assert done.status_code == 200, done.text
    assert done.json()["title"] == "Draft title" and done.json()["title_status"] == "validated"
    edited = await client.post(
        f"/v1/narratives/{public}/title", json={"title": "Fixed"}, headers=h(bot, REVIEWER)
    )
    assert edited.json()["title"] == "Fixed" and edited.json()["title_status"] == "validated"
    unknown = await client.post(
        f"/v1/narratives/{UNKNOWN}/title", json={}, headers=h(bot, REVIEWER)
    )
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "TWR-FCK-043"
    assert (await client.post(f"/v1/narratives/{public}/title", json={})).status_code in (401, 403)


# --- draft and group views --------------------------------------------------------------


async def test_draft_view_has_the_narrative_block_and_coordination_signals(
    client: httpx.AsyncClient, bot: str, db: Database
) -> None:
    built = await build_draft(db, ext="1850000000000000301")
    plain = await client.get(f"/v1/drafts/{built.verdict}", headers=h(bot, REVIEWER))
    assert plain.json()["narrative"] is None and plain.json()["coordination"] == []
    nid = await build_narrative(db, built.group_id)
    await run(
        db, "UPDATE narratives SET title = 'Story', title_status = 'draft' WHERE id = :n", n=nid
    )
    for a in await authors(db, 10, age_days=1):
        await post(db, author=a, at=datetime.now(UTC) - timedelta(hours=1), gid=built.group_id)
    view = (await client.get(f"/v1/drafts/{built.verdict}", headers=h(bot, REVIEWER))).json()
    assert view["narrative"] == {
        "id": str(await narrative_public(db, nid)),
        "title": "Story",
        "state": "active",
        "groups": 1,
    }
    kinds = {s["kind"] for s in view["coordination"]}
    assert "new_accounts" in kinds
    signal = next(s for s in view["coordination"] if s["kind"] == "new_accounts")
    assert signal["count"] == 10 and signal["window_minutes"] == 7 * 24 * 60
    listed = (await client.get("/v1/drafts", headers=h(bot, REVIEWER))).json()
    assert listed[0]["narrative"]["id"] == view["narrative"]["id"]


async def test_group_detail_shows_its_narrative(
    client: httpx.AsyncClient, bot: str, db: Database
) -> None:
    built = await build_draft(db, ext="1850000000000000302")
    group = await group_public(db, built)
    assert (await client.get(f"/v1/groups/{group}", headers=h(bot, REVIEWER))).json()[
        "narrative"
    ] is None
    nid = await build_narrative(db, built.group_id)
    await run(db, "UPDATE narratives SET state = 'surge' WHERE id = :n", n=nid)
    body = (await client.get(f"/v1/groups/{group}", headers=h(bot, REVIEWER))).json()
    assert body["narrative"] == {"id": str(await narrative_public(db, nid)), "state": "surge"}
    assert uuid.UUID(body["narrative"]["id"])
