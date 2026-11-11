from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from tower.core import admin
from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain.errors import InvalidInput, PermissionDenied
from tower.core.domain.ingestion import (
    ItemInput,
    MediaInput,
    clamp_observed_at,
    validate_filters,
    validate_media,
)
from tower.core.domain.security import Actor, Permission
from tower.core.domain.text import content_hash, normalize, simhash64, simhash_bands
from tower.core.usecases import ingestion as uc
from tower.gate.ratelimit import TokenBucketLimiter

from .conftest import TokenFactory

STAFF = 222222222222222222
BASE_ID = 1856000000000000000
FR_TEXT = "Le gouvernement a annoncé que la réforme sera votée dans les prochaines semaines"


def new_token() -> str:
    return secrets.token_urlsafe(24)


def item(n: int = 0, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "platform": "x",
        "external_id": str(BASE_ID + n),
        "text": f"{FR_TEXT} numéro {n}",
        "author": {"external_id": "44196397", "handle": "example"},
        "lang_hint": None,
        "metrics": {"views": 100, "likes": 5},
    }
    base.update(overrides)
    return base


def ext(token: str) -> dict[str, str]:
    return {"X-Installation-Token": token}


async def post(
    client: httpx.AsyncClient,
    items: list[dict[str, Any]],
    token: str | None = None,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> httpx.Response:
    hdrs = dict(headers or {})
    if token is not None:
        hdrs.update(ext(token))
    return await client.post(
        "/v1/ingest/observations", json={"items": items, **extra}, headers=hdrs
    )


async def scalar(db: Database, sql: str, **params: Any) -> Any:
    async with db.session() as session:
        return (await session.execute(text(sql), params)).scalar_one()


async def rows(db: Database, sql: str, **params: Any) -> list[Any]:
    async with db.session() as session:
        return list((await session.execute(text(sql), params)).all())


# --- Pure domain ----------------------------------------------------------------------


def test_clamp_observed_at() -> None:
    now = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
    assert clamp_observed_at(now - timedelta(hours=23), now) == now - timedelta(hours=23)
    assert clamp_observed_at(now - timedelta(hours=25), now) == now
    assert clamp_observed_at(now + timedelta(minutes=4), now) == now + timedelta(minutes=4)
    assert clamp_observed_at(now + timedelta(minutes=6), now) == now
    assert clamp_observed_at(None, now) == now
    naive = datetime(2026, 11, 11, 11, 0)
    assert clamp_observed_at(naive, now) == naive.replace(tzinfo=UTC)


def test_validate_media() -> None:
    ok = MediaInput("image", "https://example.org/a.png")
    assert validate_media(ok) is ok
    for bad in (
        MediaInput("gif", "https://example.org/a"),
        MediaInput("image", "ftp://example.org/a"),
        MediaInput("image", "javascript:alert(1)"),
        MediaInput("image", "https://example.org/" + "a" * 2100),
        MediaInput("image", ""),
    ):
        with pytest.raises(InvalidInput):
            validate_media(bad)


def test_validate_filters() -> None:
    ok = validate_filters(
        {
            "keywords": ["élection"],
            "accounts": [{"platform": "x", "external_id": "44196397"}],
            "thresholds": {"min_views": 100, "ratio": 0.5},
        }
    )
    assert ok["keywords"] == ["élection"]
    assert validate_filters({}) == {"keywords": [], "accounts": [], "thresholds": {}}
    bad: list[object] = [
        [],
        {"other": 1},
        {"keywords": "x"},
        {"keywords": [1]},
        {"keywords": [""]},
        {"keywords": ["a" * 101]},
        {"keywords": ["a"] * 501},
        {"accounts": [{"platform": "x"}]},
        {"accounts": [{"platform": "x", "external_id": "44196397", "extra": 1}]},
        {"accounts": [{"platform": "x", "external_id": "abc"}]},
        {"accounts": [{"platform": "x", "external_id": "1"}] * 2001},
        {"thresholds": {"a": "1"}},
        {"thresholds": {"a": True}},
        {"thresholds": {"a": float("inf")}},
        {"thresholds": []},
    ]
    for value in bad:
        with pytest.raises(InvalidInput):
            validate_filters(value)
    with pytest.raises(InvalidInput) as exc:
        validate_filters({"other": 1})
    assert exc.value.code == "TWR-ING-011"


# --- Rate limiter ---------------------------------------------------------------------


def test_limiter_burst_and_refill() -> None:
    lim = TokenBucketLimiter(capacity=3, refill_per_second=0.5)
    assert [lim.allow("a", now=0.0) for _ in range(4)] == [True, True, True, False]
    assert lim.allow("b", now=0.0)
    assert not lim.allow("a", now=1.0)  # 0.5 token
    assert lim.allow("a", now=2.0)  # 1 token
    assert not lim.allow("a", now=2.0)
    # Never exceeds the capacity.
    assert [lim.allow("a", now=1000.0) for _ in range(4)] == [True, True, True, False]


def test_limiter_eviction_is_bounded() -> None:
    lim = TokenBucketLimiter(capacity=1, refill_per_second=0, max_entries=3)
    for i in range(10):
        lim.allow(f"k{i}", now=0.0)
    assert len(lim) == 3
    assert not lim.allow("k9", now=0.0)  # recent entries are kept
    assert lim.allow("k0", now=0.0)  # evicted entries start over


# --- Use cases --------------------------------------------------------------------------


def to_inputs(raw: list[dict[str, Any]]) -> list[ItemInput]:
    from tower.gate.routes.ingest import ItemIn

    return [ItemIn.model_validate(r).to_domain() for r in raw]


async def run_ingest(
    db: Database,
    raw: list[dict[str, Any]],
    *,
    actor: Actor | None = None,
    token: str | None = None,
    source: str = "radar",
    quota: int = 500,
    max_items: int = 50,
    now: datetime | None = None,
) -> Any:
    actor = actor or Actor.installation()
    if token is None and actor.kind.value == "installation":
        token = new_token()
    async with db.transaction() as session:
        return await uc.ingest_items(
            session,
            actor,
            to_inputs(raw),
            source=source,
            filter_version="v1",
            installation_token=token,
            max_items=max_items,
            daily_quota=quota,
            now=now,
        )


async def test_nominal_batch(db: Database) -> None:
    raw = [item(0), item(1, lang_hint="en", text="Hello world")]
    res = await run_ingest(db, raw)
    assert (res.accepted, res.new, res.duplicates, res.rejected) == (2, 2, 0, 0)

    row = (
        await rows(
            db,
            "SELECT * FROM content_items WHERE external_id = :e",
            e=str(BASE_ID),
        )
    )[0]._mapping
    norm = normalize(raw[0]["text"])
    assert row["text"] == raw[0]["text"]
    assert row["lang"] == "fr"
    assert bytes(row["content_hash"]) == content_hash(norm)
    assert row["simhash"] == simhash64(norm)
    bands = simhash_bands(row["simhash"])
    assert (row["simhash_b0"], row["simhash_b1"], row["simhash_b2"], row["simhash_b3"]) == bands
    assert row["observation_count"] == 1
    assert row["metrics"] == {"views": 100, "likes": 5}
    assert row["author_account_id"] is not None
    en = await scalar(
        db, "SELECT lang FROM content_items WHERE external_id = :e", e=str(BASE_ID + 1)
    )
    assert en == "en"
    assert await scalar(db, "SELECT count(*) FROM observations") == 2
    obs = (
        await rows(
            db, "SELECT source, platform, filter_version, installation_pseudonym FROM observations"
        )
    )[0]
    assert obs.source == "extension" and obs.platform == "x" and obs.filter_version == "v1"
    assert len(bytes(obs.installation_pseudonym)) == 16
    acc = await rows(db, "SELECT platform, external_id, handle FROM platform_accounts")
    assert [(a.platform, a.external_id, a.handle) for a in acc] == [("x", "44196397", "example")]
    # X id expression usable (functional index expression).
    found = await scalar(
        db,
        "SELECT count(*) FROM content_items WHERE platform = 'x' AND external_id::bigint = :i",
        i=BASE_ID,
    )
    assert found == 1


async def test_duplicates_resend(db: Database) -> None:
    raw = [item(0), item(1)]
    first = await run_ingest(db, raw)
    assert first.new == 2
    changed = [
        item(0, text="EDITED text that must not replace the original", metrics={"views": 999}),
        item(1, metrics={}),
    ]
    second = await run_ingest(db, changed)
    assert (second.accepted, second.new, second.duplicates) == (2, 0, 2)
    r0 = (await rows(db, "SELECT * FROM content_items WHERE external_id = :e", e=str(BASE_ID)))[
        0
    ]._mapping
    assert r0["observation_count"] == 2
    assert r0["metrics"] == {"views": 999}
    assert r0["text"] == raw[0]["text"]
    assert r0["metrics_at"] is not None
    r1 = (await rows(db, "SELECT * FROM content_items WHERE external_id = :e", e=str(BASE_ID + 1)))[
        0
    ]._mapping
    assert r1["metrics"] == {"views": 100, "likes": 5}  # empty metrics do not erase
    assert await scalar(db, "SELECT count(*) FROM observations") == 4
    assert await scalar(db, "SELECT count(*) FROM content_items") == 2


async def test_in_batch_duplicate(db: Database) -> None:
    res = await run_ingest(db, [item(0), item(0, metrics={"views": 7}), item(1)])
    assert (res.accepted, res.new, res.duplicates, res.rejected) == (3, 2, 1, 0)
    r = (
        await rows(
            db,
            "SELECT observation_count, metrics FROM content_items WHERE external_id = :e",
            e=str(BASE_ID),
        )
    )[0]
    assert r.observation_count == 2 and r.metrics == {"views": 7}
    assert await scalar(db, "SELECT count(*) FROM observations") == 3
    assert await scalar(db, "SELECT count(*) FROM events WHERE topic = 'content.ingested'") == 2


async def test_events_and_jobs_only_for_new(db: Database) -> None:
    await run_ingest(db, [item(0), item(1)])
    evs = await rows(
        db,
        "SELECT payload, aggregate_type, aggregate_id, actor FROM events "
        "WHERE topic = 'content.ingested' ORDER BY id",
    )
    assert len(evs) == 2
    p = evs[0].payload
    assert set(p) == {"id", "public_id", "platform", "external_id", "lang", "source"}
    assert p["external_id"] == str(BASE_ID) and p["source"] == "extension" and p["lang"] == "fr"
    assert isinstance(p["id"], str) and evs[0].aggregate_id == p["public_id"]
    jobs = await rows(db, "SELECT queue, kind, payload, dedupe_key FROM jobs ORDER BY id")
    assert [(j.queue, j.kind) for j in jobs] == [("ingest", "ingest.link_copies")] * 2
    assert jobs[0].dedupe_key == f"copies:{jobs[0].payload['content_id']}"
    await run_ingest(db, [item(0), item(1)])
    assert await scalar(db, "SELECT count(*) FROM events WHERE topic = 'content.ingested'") == 2
    assert await scalar(db, "SELECT count(*) FROM jobs") == 2


async def test_per_item_rejection(db: Database) -> None:
    raw = [
        item(0),
        item(1, platform="myspace"),
        item(2, external_id="12abc"),
        item(3, external_id=str(2**63)),
        item(4, author={"external_id": "nope", "handle": "x"}),
        item(5, text="x" * 20_001),
        item(6, media=[{"kind": "image", "url": "ftp://bad"}]),
        item(7),
    ]
    res = await run_ingest(db, raw)
    assert (res.accepted, res.new, res.rejected) == (2, 2, 6)
    codes = dict(res.errors)
    assert codes[1] == "TWR-ING-005"
    assert codes[2] == "TWR-ING-006" and codes[3] == "TWR-ING-006" and codes[4] == "TWR-ING-006"
    assert codes[5] == "TWR-GEN-001" and codes[6] == "TWR-GEN-001"
    assert await scalar(db, "SELECT count(*) FROM content_items") == 2


async def test_all_rejected_does_not_consume_quota(db: Database) -> None:
    res = await run_ingest(db, [item(0, platform="nope")], quota=1)
    assert res.accepted == 0 and res.rejected == 1
    assert await scalar(db, "SELECT count(*) FROM installation_daily") == 0


async def test_batch_too_large_use_case(db: Database) -> None:
    with pytest.raises(InvalidInput) as exc:
        await run_ingest(db, [item(i) for i in range(3)], max_items=2)
    assert exc.value.code == "TWR-ING-003"
    assert await scalar(db, "SELECT count(*) FROM content_items") == 0


async def test_observed_at_clamping(db: Database) -> None:
    now = datetime.now(UTC)
    good = now - timedelta(hours=2)
    await run_ingest(
        db,
        [
            item(0, observed_at=good.isoformat()),
            item(1, observed_at=(now - timedelta(days=3)).isoformat()),
            item(2, observed_at=(now + timedelta(hours=1)).isoformat()),
            item(3),
        ],
        now=now,
    )
    seen = {
        r.external_id: r.observed_at
        for r in await rows(
            db,
            "SELECT c.external_id, o.observed_at FROM observations o "
            "JOIN content_items c ON c.id = o.content_id",
        )
    }
    assert seen[str(BASE_ID)] == good
    for n in (1, 2, 3):
        assert seen[str(BASE_ID + n)] == now


async def test_media_dedup_by_url(db: Database) -> None:
    url = "https://pbs.example.org/media/a.jpg"
    await run_ingest(
        db,
        [
            item(0, media=[{"kind": "image", "url": url}, {"kind": "video", "url": url + "2"}]),
            item(1, media=[{"kind": "image", "url": url}]),
        ],
    )
    await run_ingest(db, [item(0, media=[{"kind": "image", "url": url}])])
    assert await scalar(db, "SELECT count(*) FROM media_assets") == 2
    assert await scalar(db, "SELECT count(*) FROM content_media") == 3
    sha = await scalar(db, "SELECT url_sha256 FROM media_assets WHERE url = :u", u=url)
    assert bytes(sha) == sha256(url.encode()).digest()


async def test_quota_use_case_and_independent_tokens(db: Database) -> None:
    tok = new_token()
    await run_ingest(db, [item(i) for i in range(3)], token=tok, quota=5)
    with pytest.raises(InvalidInput) as exc:
        await run_ingest(db, [item(i) for i in range(10, 13)], token=tok, quota=5)
    assert exc.value.code == "TWR-ING-002"
    assert await scalar(db, "SELECT count(*) FROM content_items") == 3
    await run_ingest(db, [item(10), item(11)], token=tok, quota=5)  # exactly fills it
    assert await scalar(db, "SELECT items FROM installation_daily") == 5
    await run_ingest(db, [item(20)], token=new_token(), quota=5)  # independent
    assert await scalar(db, "SELECT count(*) FROM installation_daily") == 2
    with pytest.raises(InvalidInput):  # a batch larger than the quota can never fit
        await run_ingest(db, [item(i) for i in range(30, 37)], token=new_token(), quota=5)


async def test_service_actor_sources_and_permission(db: Database) -> None:
    radar = Actor.service("radar", frozenset({Permission.CONTENT_INGEST}))
    res = await run_ingest(db, [item(0)], actor=radar, source="radar")
    assert res.new == 1
    assert await scalar(db, "SELECT source FROM observations") == "radar"
    assert await scalar(db, "SELECT installation_pseudonym FROM observations") is None
    assert await scalar(db, "SELECT count(*) FROM installation_daily") == 0
    with pytest.raises(InvalidInput):
        await run_ingest(db, [item(1)], actor=radar, source="extension")
    with pytest.raises(PermissionDenied):
        await run_ingest(db, [item(1)], actor=Actor.anonymous(), source="radar")


async def test_missing_partition(db: Database) -> None:
    now = datetime.now(UTC)
    far = now + timedelta(days=4000)
    with pytest.raises(Exception) as exc:  # noqa: PT011
        await run_ingest(db, [item(0)], now=far)
    assert getattr(exc.value, "code", None) == "TWR-ING-010"
    assert await scalar(db, "SELECT count(*) FROM content_items") == 0


BYTEA_SQL = "SELECT count(*) FROM {table} WHERE position(:raw IN {name}) > 0"
TEXT_SQL = "SELECT count(*) FROM {table} WHERE strpos({name}::text, :t) > 0"


async def test_token_never_stored(db: Database, client: httpx.AsyncClient) -> None:
    tok = new_token()
    r = await post(client, [item(0, media=[{"kind": "image", "url": "https://e.org/a.png"}])], tok)
    assert r.status_code == 202, r.text
    await post(client, [item(0, platform="bad")], tok)
    raw = tok.encode()
    tables = [
        "observations", "content_items", "platform_accounts", "installation_daily",
        "installation_secrets", "media_assets", "content_media", "events", "jobs",
        "error_reports", "extension_filter_versions", "watched_accounts",
    ]  # fmt: skip
    async with db.session() as session:
        for table in tables:
            cols = (
                await session.execute(
                    text(
                        "SELECT column_name, data_type FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = :t"
                    ),
                    {"t": table},
                )
            ).all()
            assert cols, table
            for name, dtype in cols:
                if dtype == "bytea":
                    hit = await session.scalar(
                        text(BYTEA_SQL.format(table=table, name=name)),
                        {"raw": raw},
                    )
                elif dtype in ("text", "character varying", "jsonb", "json"):
                    hit = await session.scalar(
                        text(TEXT_SQL.format(table=table, name=name)),
                        {"t": tok},
                    )
                else:
                    continue
                assert hit == 0, f"token found in {table}.{name}"
    assert await scalar(db, "SELECT count(*) FROM observations") == 1


# --- Endpoints --------------------------------------------------------------------------


async def test_endpoint_nominal(client: httpx.AsyncClient, db: Database) -> None:
    tok = new_token()
    r = await post(
        client,
        [item(0), item(1, external_id="bad"), item(2)],
        tok,
        filter_version="2026-11-11.1",
    )
    assert r.status_code == 202, r.text
    assert r.json() == {
        "accepted": 2,
        "new": 2,
        "duplicates": 0,
        "rejected": 1,
        "errors": [{"index": 1, "code": "TWR-ING-006"}],
    }
    assert await scalar(db, "SELECT filter_version FROM observations LIMIT 1") == "2026-11-11.1"
    r = await post(client, [item(0)], tok)
    assert r.json()["duplicates"] == 1


async def test_endpoint_validation(client: httpx.AsyncClient) -> None:
    tok = new_token()
    r = await client.post("/v1/ingest/observations", content=b"{not json", headers=ext(tok))
    assert r.status_code == 422 and r.json()["error"]["code"] == "TWR-GEN-001"
    r = await post(client, [item(0, external_id=123)], tok)  # ids must be strings
    assert r.status_code == 422
    r = await post(client, [item(0, surprise=1)], tok)
    assert r.status_code == 422


async def test_endpoint_batch_too_large(client: httpx.AsyncClient, db: Database) -> None:
    r = await post(client, [item(i) for i in range(51)], new_token())
    assert r.status_code == 422 and r.json()["error"]["code"] == "TWR-ING-003"
    assert await scalar(db, "SELECT count(*) FROM content_items") == 0


async def test_endpoint_body_too_large(client: httpx.AsyncClient) -> None:
    tok = new_token()
    big = json.dumps({"items": [item(0, text="x" * 300_000)]})
    r = await client.post("/v1/ingest/observations", content=big, headers=ext(tok))
    assert r.status_code == 413 and r.json()["error"]["code"] == "TWR-ING-004"

    async def chunks() -> AsyncIterator[bytes]:  # no Content-Length: size enforced while reading
        for _ in range(40):
            yield b" " * 10_000

    r = await client.post("/v1/ingest/observations", content=chunks(), headers=ext(tok))
    assert r.status_code == 413 and r.json()["error"]["code"] == "TWR-ING-004"


async def test_endpoint_auth(
    client: httpx.AsyncClient, make_token: TokenFactory, db: Database
) -> None:
    for bad in ("short", "has space and !!! symbols ........", "a" * 65, ""):
        r = await post(client, [item(0)], bad)
        assert r.status_code == 401 and r.json()["error"]["code"] == "TWR-ING-001", bad
    assert await scalar(db, "SELECT count(*) FROM content_items") == 0

    # Anonymous without any credential: missing permission.
    r = await post(client, [item(0)])
    assert r.status_code == 403

    radar = await make_token("radar", [Permission.CONTENT_INGEST])
    r = await post(client, [item(0)], headers={"Authorization": f"Bearer {radar}"}, source="feed")
    assert r.status_code == 202 and r.json()["new"] == 1
    assert await scalar(db, "SELECT source FROM observations") == "feed"
    r = await post(client, [item(1)], headers={"Authorization": f"Bearer {radar}"})
    assert r.status_code == 202
    assert await scalar(db, "SELECT count(*) FROM observations WHERE source = 'radar'") == 1
    r = await post(
        client, [item(2)], headers={"Authorization": f"Bearer {radar}"}, source="extension"
    )
    assert r.status_code == 422

    other = await make_token("other", [Permission.DOSSIER_READ])
    r = await post(client, [item(3)], headers={"Authorization": f"Bearer {other}"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "TWR-AUTH-005"


async def test_endpoint_quota(client: httpx.AsyncClient, db: Database) -> None:
    tok = new_token()
    assert (await post(client, [item(i) for i in range(50)], tok)).status_code == 202
    # 9 more full batches reach 500 exactly (limiter allows a burst of 20).
    for b in range(1, 10):
        r = await post(client, [item(b * 100 + i) for i in range(50)], tok)
        assert r.status_code == 202, r.text
    assert await scalar(db, "SELECT items FROM installation_daily") == 500
    before = await scalar(db, "SELECT count(*) FROM content_items")
    r = await post(client, [item(5000)], tok)
    assert r.status_code == 429 and r.json()["error"]["code"] == "TWR-ING-002"
    assert await scalar(db, "SELECT count(*) FROM content_items") == before
    assert await scalar(db, "SELECT count(*) FROM observations") == 500
    assert await scalar(db, "SELECT items FROM installation_daily") == 500
    # Another installation is independent.
    r = await post(client, [item(6000)], new_token())
    assert r.status_code == 202


async def test_endpoint_quota_partial_refusal_writes_nothing(
    client: httpx.AsyncClient, db: Database
) -> None:
    tok = new_token()
    async with db.transaction() as session:
        today = datetime.now(UTC).date()
        from tower.core.domain.privacy import installation_pseudonym
        from tower.core.usecases.installations import daily_secret

        pseud = installation_pseudonym(await daily_secret(session, today), tok)
        await session.execute(
            text(
                "INSERT INTO installation_daily (day, pseudonym, items, quota) "
                "VALUES (:d, :p, 480, 500)"
            ),
            {"d": today, "p": pseud},
        )
    r = await post(client, [item(i) for i in range(30)], tok)
    assert r.status_code == 429
    assert await scalar(db, "SELECT count(*) FROM content_items") == 0
    assert await scalar(db, "SELECT count(*) FROM events WHERE topic = 'content.ingested'") == 0
    assert await scalar(db, "SELECT items FROM installation_daily") == 480
    assert (await post(client, [item(i) for i in range(20)], tok)).status_code == 202


async def test_endpoint_rate_limited(client: httpx.AsyncClient, db: Database) -> None:
    app = client._transport.app  # type: ignore[attr-defined]
    app.state.limiter = TokenBucketLimiter(capacity=2, refill_per_second=0.0001)
    tok = new_token()
    assert (await post(client, [item(0)], tok)).status_code == 202
    assert (await post(client, [item(1)], tok)).status_code == 202
    r = await post(client, [item(2)], tok)
    assert r.status_code == 429 and r.json()["error"]["code"] == "TWR-ING-007"
    assert await scalar(db, "SELECT count(*) FROM content_items") == 2
    # Another address (last X-Forwarded-For entry) has its own bucket.
    r = await client.post(
        "/v1/ingest/observations",
        json={"items": [item(3)]},
        headers={**ext(tok), "X-Forwarded-For": "1.2.3.4, 9.9.9.9"},
    )
    assert r.status_code == 202
    r = await client.post(
        "/v1/ingest/observations",
        json={"items": [item(4)]},
        headers={**ext(tok), "X-Forwarded-For": "6.6.6.6, 9.9.9.9"},
    )
    assert r.status_code == 202  # same last entry: second token of that bucket
    r = await client.post(
        "/v1/ingest/observations",
        json={"items": [item(5)]},
        headers={**ext(tok), "X-Forwarded-For": "7.7.7.7, 9.9.9.9"},
    )
    assert r.status_code == 429


async def test_config_endpoint(client: httpx.AsyncClient, db: Database) -> None:
    r = await client.get("/v1/ingest/config")
    assert r.status_code == 200
    assert r.json() == {
        "version": None,
        "filters": {"keywords": [], "accounts": [], "thresholds": {}},
    }
    assert r.headers["cache-control"] == "public, max-age=300"
    empty_etag = r.headers["etag"]

    filters = {"keywords": ["élection"], "accounts": [{"platform": "x", "external_id": "44196397"}]}
    async with db.transaction() as session:
        await uc.publish_filters(
            session, Actor.system("test"), version="2026-11-11.1", filters=filters
        )
    r = await client.get("/v1/ingest/config")
    body = r.json()
    assert body["version"] == "2026-11-11.1"
    assert body["filters"]["keywords"] == ["élection"] and body["filters"]["thresholds"] == {}
    etag = r.headers["etag"]
    assert etag != empty_etag and etag.startswith('W/"') and len(etag) == 3 + 16 + 1
    assert r.headers["cache-control"] == "public, max-age=300"

    r = await client.get("/v1/ingest/config", headers={"If-None-Match": etag})
    assert r.status_code == 304 and r.content == b"" and r.headers["etag"] == etag
    assert r.headers["cache-control"] == "public, max-age=300"
    r = await client.get("/v1/ingest/config", headers={"If-None-Match": 'W/"nope"'})
    assert r.status_code == 200

    # New version replaces the active one.
    async with db.transaction() as session:
        await uc.publish_filters(session, Actor.system("test"), version="2026-11-11.2", filters={})
    assert (await client.get("/v1/ingest/config")).json()["version"] == "2026-11-11.2"
    assert await scalar(db, "SELECT count(*) FROM extension_filter_versions WHERE active") == 1
    assert (
        await scalar(db, "SELECT count(*) FROM events WHERE topic = 'extension_filters.published'")
        == 2
    )


async def test_publish_filters_errors(db: Database) -> None:
    async with db.transaction() as session:
        with pytest.raises(PermissionDenied):
            await uc.publish_filters(
                session,
                Actor.service("x", frozenset()),
                version="v",
                filters={},
            )
        with pytest.raises(InvalidInput):
            await uc.publish_filters(session, Actor.system("t"), version="v", filters={"x": 1})
        await uc.publish_filters(session, Actor.system("t"), version="v", filters={})
        with pytest.raises(InvalidInput):
            await uc.publish_filters(session, Actor.system("t"), version="v", filters={})


async def test_publish_filters_from_file(
    db: Database, settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "filters.json"
    path.write_text(json.dumps({"keywords": ["a", "b"], "thresholds": {"n": 1}}))
    await admin.publish_filters_from_file(settings, str(path), "v-file")
    assert "v-file" in capsys.readouterr().out
    async with db.session() as session:
        version, filters = await uc.get_active_filters(session)
    assert version == "v-file" and filters["keywords"] == ["a", "b"]
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    with pytest.raises(InvalidInput):
        await admin.publish_filters_from_file(settings, str(bad), "v2")


# --- Watchlist --------------------------------------------------------------------------


@pytest.fixture
async def staff_headers(
    make_token: TokenFactory, add_staff: Callable[..., Coroutine[Any, Any, None]]
) -> Callable[[str], dict[str, str]]:
    token = await make_token()
    await add_staff(STAFF, ["editor"])
    await add_staff(STAFF + 1, ["reviewer"])

    def _headers(who: str) -> dict[str, str]:
        discord = STAFF if who == "editor" else STAFF + 1
        return {"Authorization": f"Bearer {token}", "X-Acting-Discord-Id": str(discord)}

    return _headers


async def test_watchlist_permissions_and_crud(
    client: httpx.AsyncClient, db: Database, staff_headers: Callable[[str], dict[str, str]]
) -> None:
    editor, reviewer = staff_headers("editor"), staff_headers("reviewer")
    body = {
        "platform": "x",
        "external_id": "44196397",
        "handle": "@example",
        "category": "journalist",
        "priority": 10,
        "note": "test",
    }
    r = await client.post("/v1/watched-accounts", json=body, headers=reviewer)
    assert r.status_code == 403
    r = await client.post("/v1/watched-accounts", json=body, headers=editor)
    assert r.status_code == 201, r.text
    w = r.json()
    assert w["platform"] == "x" and w["external_id"] == "44196397" and w["handle"] == "example"
    assert w["version"] == 1 and w["active"] is True and w["priority"] == 10
    pid = w["public_id"]

    # Duplicate.
    r = await client.post("/v1/watched-accounts", json=body, headers=editor)
    assert r.status_code == 409 and r.json()["error"]["code"] == "TWR-ING-009"

    # Validation.
    bad_cat = {**body, "external_id": "1", "category": "left_wing"}
    assert (
        await client.post("/v1/watched-accounts", json=bad_cat, headers=editor)
    ).status_code == 422
    bad_prio = {**body, "external_id": "1", "priority": 101}
    assert (
        await client.post("/v1/watched-accounts", json=bad_prio, headers=editor)
    ).status_code == 422
    bad_id = {**body, "external_id": "abc"}
    r = await client.post("/v1/watched-accounts", json=bad_id, headers=editor)
    assert r.json()["error"]["code"] == "TWR-ING-006"
    bad_plat = {**body, "platform": "nope"}
    r = await client.post("/v1/watched-accounts", json=bad_plat, headers=editor)
    assert r.json()["error"]["code"] == "TWR-ING-005"

    # Read.
    r = await client.get("/v1/watched-accounts", headers=reviewer)
    assert r.status_code == 200 and [x["public_id"] for x in r.json()] == [pid]
    assert (await client.get("/v1/watched-accounts")).status_code == 403

    # Update.
    r = await client.patch(
        f"/v1/watched-accounts/{pid}",
        json={"version": 1, "priority": -5, "active": False, "category": "media_outlet"},
        headers=reviewer,
    )
    assert r.status_code == 403
    r = await client.patch(
        f"/v1/watched-accounts/{pid}",
        json={"version": 1, "priority": -5, "active": False, "category": "media_outlet"},
        headers=editor,
    )
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2 and r.json()["priority"] == -5 and r.json()["active"] is False
    r = await client.patch(
        f"/v1/watched-accounts/{pid}", json={"version": 1, "priority": 1}, headers=editor
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "TWR-GEN-003"
    r = await client.patch(
        "/v1/watched-accounts/00000000-0000-0000-0000-000000000000",
        json={"version": 1},
        headers=editor,
    )
    assert r.status_code == 404 and r.json()["error"]["code"] == "TWR-ING-008"
    r = await client.patch(
        f"/v1/watched-accounts/{pid}", json={"version": 2, "handle": "renamed"}, headers=editor
    )
    assert r.json()["handle"] == "renamed" and r.json()["version"] == 3

    # Filters.
    assert (await client.get("/v1/watched-accounts?active=true", headers=editor)).json() == []
    assert len((await client.get("/v1/watched-accounts?active=false", headers=editor)).json()) == 1
    assert (await client.get("/v1/watched-accounts?platform=bluesky", headers=editor)).json() == []
    assert (
        len((await client.get("/v1/watched-accounts?platform=x&limit=1", headers=editor)).json())
        == 1
    )
    assert (await client.get("/v1/watched-accounts?offset=1", headers=editor)).json() == []

    # Audit and events.
    audit = await rows(db, "SELECT action, actor, changes FROM watched_account_events ORDER BY id")
    assert [a.action for a in audit] == ["added", "updated", "updated"]
    assert all(a.actor == f"staff:{STAFF}@intercom" for a in audit)
    assert audit[1].changes["priority"] == {"from": 10, "to": -5}
    evs = await rows(db, "SELECT payload FROM events WHERE topic = 'watchlist.changed' ORDER BY id")
    assert [e.payload["action"] for e in evs] == ["added", "updated", "updated"]
    assert evs[0].payload["watched_account"]["public_id"] == pid
    assert evs[1].payload["watched_account"]["version"] == 2
    assert evs[2].payload["watched_account"]["handle"] == "renamed"


async def test_watchlist_reuses_ingested_account(
    client: httpx.AsyncClient, db: Database, staff_headers: Callable[[str], dict[str, str]]
) -> None:
    await post(client, [item(0)], new_token())  # creates platform account 44196397
    r = await client.post(
        "/v1/watched-accounts",
        json={"platform": "x", "external_id": "44196397", "category": "other"},
        headers=staff_headers("editor"),
    )
    assert r.status_code == 201 and r.json()["handle"] == "example"
    assert await scalar(db, "SELECT count(*) FROM platform_accounts") == 1


def test_today_is_utc_date() -> None:
    assert isinstance(datetime.now(UTC).date(), date)
