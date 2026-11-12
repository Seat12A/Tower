"""Coordination signals on seeded data: each kind, thresholds and negatives (real DB)."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from tower.core.db.database import Database
from tower.core.factcheck.coordination import coordination_signals
from tower.core.models.ingestion import ContentItem, ContentMedia, MediaAsset, PlatformAccount

from .test_factcheck_matching import _reset  # noqa: F401  (autouse fixture)
from .test_factcheck_narratives import link_content, seed, unit

NOW = datetime.now(UTC)
_counter = {"n": 0}


def _next() -> int:
    _counter["n"] += 1
    return _counter["n"]


async def account(db: Database, *, age_days: float = 0) -> int:
    n = _next()
    async with db.transaction() as s:
        row = PlatformAccount(platform="x", external_id=str(8_000_000 + n), handle=f"acc{n}")
        s.add(row)
        await s.flush()
        await s.execute(
            text("UPDATE platform_accounts SET created_at = :t WHERE id = :i"),
            {"t": NOW - timedelta(days=age_days), "i": row.id},
        )
        return row.id


async def post(
    db: Database,
    *,
    author: int | None,
    at: datetime,
    gid: int | None = None,
    body: str | None = None,
    copy_of: int | None = None,
) -> int:
    """A post (unique unless ``body`` repeats), linked to the group when given."""
    n = _next()
    body = body if body is not None else f"unique post {n}"
    async with db.transaction() as s:
        row = ContentItem(
            platform="x",
            external_id=str(9_100_000 + n),
            author_account_id=author,
            text=body,
            lang="fr",
            lang_confidence=0.99,
            useful_words=5,
            content_hash=hashlib.sha256(body.encode()).digest(),
            simhash=n,
            copy_of_id=copy_of,
            created_at_platform=at,
        )
        s.add(row)
        await s.flush()
        content_id = row.id
    if gid is not None:
        await link_content(db, content_id, gid)
    return content_id


async def signals(db: Database, gid: int) -> dict[str, dict]:  # type: ignore[type-arg]
    async with db.session() as s:
        found = await coordination_signals(s, group_id=gid, now=NOW)
    return {f["kind"]: f for f in found}


async def new_group(db: Database, axis: int = 300) -> int:
    return await seed(db, f"g{axis}", unit(axis))


async def authors(db: Database, count: int, *, age_days: float = 60) -> list[int]:
    return [await account(db, age_days=age_days) for _ in range(count)]


# --- near_identical_burst ---------------------------------------------------------------


async def test_ten_accounts_posting_the_same_text_within_ten_minutes_is_a_burst(
    db: Database,
) -> None:
    gid = await new_group(db)
    accounts = await authors(db, 10)
    # Only the first post is linked to the group: the others are copies (same hash).
    await post(db, author=accounts[0], at=NOW - timedelta(minutes=30), gid=gid, body="same")
    for i, a in enumerate(accounts[1:]):
        await post(db, author=a, at=NOW - timedelta(minutes=30 - i * 0.5), body="same")
    found = await signals(db, gid)
    burst = found["near_identical_burst"]
    assert burst["count"] == 10 and burst["window_minutes"] == 10
    assert burst["detail"]["basis"] == "content_hash"
    assert set(found) == {"near_identical_burst"}


async def test_copies_flagged_by_copy_of_count_even_with_different_texts(db: Database) -> None:
    gid = await new_group(db)
    accounts = await authors(db, 11)
    root = await post(db, author=accounts[0], at=NOW - timedelta(minutes=20), gid=gid)
    for i, a in enumerate(accounts[1:]):
        await post(db, author=a, at=NOW - timedelta(minutes=19, seconds=-i * 20), copy_of=root)
    burst = (await signals(db, gid))["near_identical_burst"]
    assert burst["count"] == 11 and burst["detail"]["basis"] == "copy_of"


async def test_nine_accounts_are_not_a_burst(db: Database) -> None:
    gid = await new_group(db)
    accounts = await authors(db, 9)
    for a in accounts:
        await post(db, author=a, at=NOW - timedelta(minutes=10), gid=gid, body="same")
    assert "near_identical_burst" not in await signals(db, gid)


async def test_the_same_accounts_reposting_do_not_inflate_the_count(db: Database) -> None:
    gid = await new_group(db)
    accounts = await authors(db, 3)
    for i in range(12):
        await post(db, author=accounts[i % 3], at=NOW - timedelta(minutes=10), gid=gid, body="same")
    assert "near_identical_burst" not in await signals(db, gid)


async def test_ten_accounts_spread_over_an_hour_are_not_a_burst(db: Database) -> None:
    gid = await new_group(db)
    accounts = await authors(db, 10)
    for i, a in enumerate(accounts):
        await post(db, author=a, at=NOW - timedelta(minutes=90 - i * 6), gid=gid, body="same")
    assert "near_identical_burst" not in await signals(db, gid)


async def test_different_texts_without_copy_link_are_not_a_burst(db: Database) -> None:
    gid = await new_group(db)
    for a in await authors(db, 12):
        await post(db, author=a, at=NOW - timedelta(minutes=10), gid=gid)
    assert await signals(db, gid) == {}


# --- new_accounts -----------------------------------------------------------------------


async def test_half_of_ten_authors_first_seen_this_week_is_flagged(db: Database) -> None:
    gid = await new_group(db)
    fresh = await authors(db, 5, age_days=1)
    old = await authors(db, 5, age_days=40)
    for a in fresh + old:
        await post(db, author=a, at=NOW - timedelta(hours=1), gid=gid)
    found = await signals(db, gid)
    signal = found["new_accounts"]
    assert signal["count"] == 5 and signal["detail"]["authors"] == 10
    assert signal["detail"]["share"] == 0.5
    assert "first seen" in signal["detail"]["note"]


async def test_new_accounts_needs_half_and_ten_authors(db: Database) -> None:
    below = await new_group(db, 310)
    for a in await authors(db, 4, age_days=1) + await authors(db, 6, age_days=40):
        await post(db, author=a, at=NOW - timedelta(hours=1), gid=below)
    assert "new_accounts" not in await signals(db, below)
    few = await new_group(db, 320)
    for a in await authors(db, 9, age_days=1):
        await post(db, author=a, at=NOW - timedelta(hours=1), gid=few)
    assert "new_accounts" not in await signals(db, few)


# --- installation_burst -----------------------------------------------------------------


async def observe(
    db: Database, content_id: int, pseudonym: bytes, *, count: int, spacing_s: float
) -> None:
    async with db.transaction() as s:
        for i in range(count):
            await s.execute(
                text(
                    "INSERT INTO observations (observed_at, content_id, platform, source, "
                    "installation_pseudonym) VALUES (:t, :c, 'x', 'extension', :p)"
                ),
                {"t": NOW - timedelta(seconds=i * spacing_s), "c": content_id, "p": pseudonym},
            )


async def test_twenty_observations_by_one_installation_in_ten_minutes_is_a_burst(
    db: Database,
) -> None:
    gid = await new_group(db)
    content = await post(db, author=None, at=NOW - timedelta(hours=1), gid=gid)
    await observe(db, content, b"\x01" * 16, count=20, spacing_s=20)
    burst = (await signals(db, gid))["installation_burst"]
    assert burst["count"] == 20 and burst["window_minutes"] == 10
    assert "pseudonym" not in str(burst)


async def test_installation_burst_negatives(db: Database) -> None:
    short = await new_group(db, 330)
    content = await post(db, author=None, at=NOW - timedelta(hours=1), gid=short)
    await observe(db, content, b"\x02" * 16, count=19, spacing_s=20)
    assert "installation_burst" not in await signals(db, short)
    spread = await new_group(db, 340)
    content = await post(db, author=None, at=NOW - timedelta(hours=1), gid=spread)
    await observe(db, content, b"\x03" * 16, count=20, spacing_s=120)
    assert "installation_burst" not in await signals(db, spread)
    split = await new_group(db, 350)
    content = await post(db, author=None, at=NOW - timedelta(hours=1), gid=split)
    await observe(db, content, b"\x04" * 16, count=12, spacing_s=20)
    await observe(db, content, b"\x05" * 16, count=12, spacing_s=20)
    assert "installation_burst" not in await signals(db, split)


# --- media_reuse ------------------------------------------------------------------------


async def attach_media(db: Database, media_id: int | None, *contents: int) -> int:
    async with db.transaction() as s:
        if media_id is None:
            media = MediaAsset(
                kind="image", url="https://img.example/m.png", url_sha256=os.urandom(32)
            )
            s.add(media)
            await s.flush()
            media_id = media.id
        for c in contents:
            s.add(ContentMedia(content_id=c, media_id=media_id, position=0))
    return media_id


async def test_one_media_in_two_groups_is_flagged_on_both(db: Database) -> None:
    a, b = await new_group(db, 360), await new_group(db, 361)
    ca = await post(db, author=None, at=NOW, gid=a)
    cb = await post(db, author=None, at=NOW, gid=b)
    await attach_media(db, None, ca, cb)
    for gid in (a, b):
        signal = (await signals(db, gid))["media_reuse"]
        assert signal["count"] == 2 and signal["window_minutes"] == 0
        assert len(signal["detail"]["other_groups"]) == 1


async def test_a_media_used_in_one_group_only_is_not_flagged(db: Database) -> None:
    a, b = await new_group(db, 370), await new_group(db, 371)
    c1 = await post(db, author=None, at=NOW, gid=a)
    c2 = await post(db, author=None, at=NOW, gid=a)
    await post(db, author=None, at=NOW, gid=b)
    await attach_media(db, None, c1, c2)
    assert await signals(db, a) == {}
    assert await signals(db, b) == {}


async def test_an_empty_group_has_no_signal(db: Database) -> None:
    assert await signals(db, await new_group(db, 380)) == {}
