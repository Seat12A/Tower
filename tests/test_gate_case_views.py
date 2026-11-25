from __future__ import annotations

import httpx
import pytest
from sqlalchemy import update

from tower.core.db.database import Database
from tower.core.models.cases import Case
from tower.core.models.factcheck import Claim

from .test_drafts_usecases import EXT, Built, build_draft
from .test_gate_factcheck import EDITOR, UNKNOWN, bot, h

__all__ = ["bot"]

OTHER = "1850000000000000077"
DRAFT_ONLY = "1850000000000000088"


async def _publish(client: httpx.AsyncClient, token: str, built: Built, n: int) -> None:
    r = await client.post(
        f"/v1/drafts/{built.verdict}/review", json={"decision": "accept"}, headers=h(token, EDITOR)
    )
    assert r.status_code == 200
    url = f"https://x.com/seat12a/status/18500000000000009{n:02d}"
    r = await client.post(
        f"/v1/cases/{built.case}/publish", json={"url": url}, headers=h(token, EDITOR)
    )
    assert r.status_code == 200, r.text


@pytest.fixture
async def published(client: httpx.AsyncClient, bot: str, db: Database) -> list[Built]:
    """Two published fact-checks (false FR statistic, true EN quote on the US) and one
    draft that must never be public."""
    first = await build_draft(db)
    second = await build_draft(db, ext=OTHER, post_lang="en", veracity="true")
    await build_draft(db, ext=DRAFT_ONLY)
    async with db.transaction() as s:
        await s.execute(
            update(Claim)
            .where(Claim.id == second.claim_id)
            .values(
                claim_type="quote",
                text_fr="Le président a déclaré la guerre au café",
                jurisdictions=["US"],
            )
        )
        await s.execute(
            update(Case)
            .where(Case.id == second.case_id)
            .values(title="Le président a déclaré la guerre au café")
        )
        await s.execute(
            update(Claim).where(Claim.id == first.claim_id).values(jurisdictions=["FR"])
        )
    await _publish(client, bot, first, 1)
    await _publish(client, bot, second, 2)
    return [first, second]


async def test_public_search_filters_and_pagination(
    client: httpx.AsyncClient, published: list[Built]
) -> None:
    r = await client.get("/v1/cases/search")
    assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=300"
    body = r.json()
    assert body["total"] == 2 and body["pages"] == 1  # the draft case is not public
    newest = body["items"][0]
    assert newest["public_id"] == str(published[1].case) and newest["veracity"] == "true"
    assert newest["status"] is None and newest["priority"] is None  # internal fields
    assert newest["url"] == f"https://x.com/i/status/{OTHER}"
    assert newest["published_at"] and newest["verdict_state"] == "current"

    async def ids(**params: object) -> list[str]:
        res = await client.get("/v1/cases/search", params=params)  # type: ignore[arg-type]
        assert res.status_code == 200, res.text
        return [item["public_id"] for item in res.json()["items"]]

    first, second = str(published[0].case), str(published[1].case)
    assert await ids(veracity="false") == [first]
    assert await ids(veracity=["false", "true"], sort="oldest") == [first, second]
    assert await ids(claim_type="quote") == [second]
    assert await ids(jurisdiction="us") == [second]
    assert await ids(lang="en") == [second]
    assert await ids(q="chômage") == [first]  # full text (French)
    assert await ids(q="chôm") == [first]  # partial word
    assert await ids(q="coffee war", sort="relevance") == []
    assert await ids(status="received") == []  # the public never sees unpublished cases

    page2 = (await client.get("/v1/cases/search", params={"per_page": 1, "page": 2})).json()
    assert page2["pages"] == 2 and page2["total"] == 2
    assert [i["public_id"] for i in page2["items"]] == [first]

    bad = await client.get("/v1/cases/search", params={"page": 1001, "per_page": 10})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "TWR-DOS-008"
    since = "2030-01-01T00:00:00Z"
    bad = await client.get("/v1/cases/search", params={"since": since, "until": "2029-01-01"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "TWR-DOS-008"
    assert (await client.get("/v1/cases/search", params={"veracity": "maybe"})).status_code == 422


async def test_team_search_sees_every_case(
    client: httpx.AsyncClient, bot: str, published: list[Built]
) -> None:
    r = await client.get("/v1/cases/search", params={"status": "received"}, headers=h(bot))
    body = r.json()
    assert r.status_code == 200 and "cache-control" not in r.headers
    assert body["total"] == 1 and body["items"][0]["status"] == "received"
    assert body["items"][0]["verdict_state"] == "draft"


async def test_case_details(client: httpx.AsyncClient, bot: str, published: list[Built]) -> None:
    built = published[1]
    r = await client.get(f"/v1/cases/{built.case}/details")
    assert r.status_code == 200
    d = r.json()
    assert d["content"] == {
        "platform": "x",
        "external_id": OTHER,
        "url": f"https://x.com/i/status/{OTHER}",
        "lang": "en",
    }
    assert d["claim"]["claim_type"] == "quote" and d["claim"]["lang"] == "en"
    assert d["verdict"]["veracity"] == "true" and d["verdict"]["state"] == "current"
    assert d["verdict"]["id"] == str(built.verdict)
    assert d["note"]["lang"] == "en" and d["note"]["links"]
    assert d["posts_count"] == 1 and d["internal"] is None
    assert [t["status"] for t in d["timeline"]][-1] == "published"
    assert d["timeline"][0]["actor"] is None  # no staff identity for the public
    assert "author" not in str(d)

    french = (await client.get(f"/v1/cases/{built.case}/details?lang=fr")).json()
    assert french["claim"]["text"].startswith("Le président") and french["note"]["lang"] == "fr"

    team = (await client.get(f"/v1/cases/{built.case}/details", headers=h(bot))).json()
    assert team["internal"]["draft_id"] == str(built.verdict) and team["status"] == "published"
    assert team["timeline"][0]["actor"]

    missing = await client.get(f"/v1/cases/{UNKNOWN}/details")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "TWR-DOS-001"


async def test_unpublished_case_details_are_hidden(
    client: httpx.AsyncClient, bot: str, db: Database
) -> None:
    built = await build_draft(db, ext=EXT)
    assert (await client.get(f"/v1/cases/{built.case}/details")).status_code == 404
    team = await client.get(f"/v1/cases/{built.case}/details", headers=h(bot))
    assert team.status_code == 200 and team.json()["verdict"]["state"] == "draft"


async def test_case_stats(client: httpx.AsyncClient, bot: str, published: list[Built]) -> None:
    r = await client.get("/v1/cases/stats")
    assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=300"
    s = r.json()
    assert s["total"] == 2 and s["by_status"] is None
    assert s["by_veracity"] == {"false": 1, "true": 1}
    assert s["by_claim_type"] == {"quote": 1, "statistic": 1}
    assert s["by_jurisdiction"] == {"FR": 1, "US": 1}
    assert s["by_kind"] == {"fact_check": 2} and s["interval"] == "month"
    assert sum(b["count"] for b in s["timeline"]) == 2

    filtered = (await client.get("/v1/cases/stats", params={"veracity": "false"})).json()
    assert filtered["total"] == 1

    team = (await client.get("/v1/cases/stats?interval=day", headers=h(bot))).json()
    assert team["total"] == 3 and team["by_status"] == {"published": 2, "received": 1}
    assert team["by_veracity"] == {"false": 1, "true": 1}  # a draft is not a verdict
    assert (await client.get("/v1/cases/stats?interval=year")).status_code == 422
