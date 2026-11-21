from __future__ import annotations

import hashlib

import httpx
from sqlalchemy import text

from tower.core.db.database import Database
from tower.core.usecases.verdict_index import bucket_of

from .test_drafts_usecases import EXT, build_draft
from .test_gate_factcheck import EDITOR, bot, h  # noqa: F401  (fixture)

OTHER = "1850000000000000099"


def sha(external_id: str) -> str:
    return hashlib.sha256(external_id.encode()).hexdigest()


def url(external_id: str, platform: str = "x") -> str:
    return f"/v1/verdict-index/{platform}/{bucket_of(external_id)}"


async def test_bucket_lists_posts_with_a_visible_verdict(
    client: httpx.AsyncClient,
    bot: str,  # noqa: F811
    db: Database,
) -> None:
    built = await build_draft(db, post_lang="en")
    await build_draft(db, ext=OTHER)  # stays a draft: never listed
    assert (await client.get(url(EXT))).json()["hashes"] == []  # a draft is never public

    r = await client.post(
        f"/v1/drafts/{built.verdict}/review", json={"decision": "accept"}, headers=h(bot, EDITOR)
    )
    assert r.status_code == 200

    anonymous = await client.get(url(EXT))
    assert anonymous.status_code == 200
    assert anonymous.headers["cache-control"] == "public, max-age=300"
    assert anonymous.json() == {"platform": "x", "bucket": bucket_of(EXT), "hashes": [sha(EXT)]}
    # The extension's match leads to the public verdict.
    assert (await client.get(f"/v1/content/x/{EXT}/verdict")).status_code == 200
    if bucket_of(OTHER) != bucket_of(EXT):
        assert (await client.get(url(OTHER))).json()["hashes"] == []

    etag = anonymous.headers["etag"]
    cached = await client.get(url(EXT), headers={"If-None-Match": etag})
    assert cached.status_code == 304 and cached.headers["etag"] == etag
    upper = await client.get(f"/v1/verdict-index/x/{bucket_of(EXT).upper()}")
    assert upper.json()["hashes"] == [sha(EXT)]

    # An expired verdict is never shown, so never listed.
    async with db.transaction() as s:
        await s.execute(
            text("UPDATE verdicts SET expires_at = now() - interval '1 minute' WHERE id = :id"),
            {"id": built.verdict_id},
        )
    assert (await client.get(url(EXT))).json()["hashes"] == []
    assert (await client.get(f"/v1/content/x/{EXT}/verdict")).status_code == 404


async def test_bad_bucket_or_platform(client: httpx.AsyncClient) -> None:
    for path in (
        "/v1/verdict-index/x/abc",
        "/v1/verdict-index/x/zzzz",
        "/v1/verdict-index/x/12345",
    ):
        r = await client.get(path)
        assert r.status_code == 422 and r.json()["error"]["code"] == "TWR-FCK-050"
    r = await client.get("/v1/verdict-index/myspace/abcd")
    assert r.status_code == 422 and r.json()["error"]["code"] == "TWR-FCK-050"


def test_bucket_matches_postgres_and_the_extension() -> None:
    # sha256 of the UTF-8 id; the first 4 hex characters name the bucket.
    assert bucket_of("1856000000000000000") == "6f58"
