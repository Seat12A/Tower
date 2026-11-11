from __future__ import annotations

import secrets as pysecrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain.privacy import installation_pseudonym
from tower.core.jobs.maintenance import run_singleton
from tower.core.jobs.queue import JobRecord
from tower.core.services.base import JobContext
from tower.core.services.registry import load_services
from tower.core.usecases.installations import daily_secret
from tower.services.ingest import QUEUE, service
from tower.services.ingest.copies import link_copies, link_copy
from tower.services.ingest.maintenance import (
    ensure_partitions,
    maintain_partitions,
    purge_partitions,
    rotate,
)

NOW = datetime.now(UTC)
_counter = 0


async def _add(
    s: AsyncSession,
    *,
    simhash: int = 0,
    content_hash: bytes | None = None,
    seen: datetime | None = None,
    copy_of: int | None = None,
) -> int:
    global _counter
    _counter += 1
    row = await s.execute(
        text(
            "INSERT INTO content_items (platform, external_id, text, lang, lang_confidence, "
            "useful_words, content_hash, simhash, first_seen_at, copy_of_id) "
            "VALUES ('x', :ext, 't', 'fr', 1, 10, :h, :sh, :seen, :copy) RETURNING id"
        ),
        {
            "ext": str(_counter),
            "h": content_hash if content_hash is not None else pysecrets.token_bytes(32),
            "sh": simhash,
            "seen": seen or NOW,
            "copy": copy_of,
        },
    )
    return int(row.scalar_one())


async def _copy_of(s: AsyncSession, content_id: int) -> int | None:
    value = (
        await s.execute(
            text("SELECT copy_of_id FROM content_items WHERE id = :i"), {"i": content_id}
        )
    ).scalar_one()
    return None if value is None else int(value)


def _ago(**kw: float) -> datetime:
    return NOW - timedelta(**kw)


async def test_exact_copy_links_to_oldest(db: Database) -> None:
    h = b"\x01" * 32
    async with db.transaction() as s:
        a = await _add(s, content_hash=h, seen=_ago(hours=3), simhash=1)
        b = await _add(s, content_hash=h, seen=_ago(hours=2), simhash=2**40)
        c = await _add(s, content_hash=h, seen=_ago(hours=1), simhash=2**50)
        assert await link_copy(s, c, now=NOW) == a
        assert await link_copy(s, b, now=NOW) == a
        assert await link_copy(s, a, now=NOW) is None
        assert await _copy_of(s, a) is None


async def test_near_copy_distance(db: Database) -> None:
    base = 0x0123456789ABCDEF
    async with db.transaction() as s:
        a = await _add(s, simhash=base, seen=_ago(hours=2))
        near = await _add(s, simhash=base ^ (1 << 3) ^ (1 << 40), seen=_ago(hours=1))
        far = await _add(s, simhash=base ^ 0b1111, seen=_ago(minutes=30))
        assert await link_copy(s, near, now=NOW) == a
        assert await link_copy(s, far, now=NOW) is None
        assert await _copy_of(s, far) is None


async def test_near_copy_negative_simhash(db: Database) -> None:
    base = -0x0123456789ABCDEF
    async with db.transaction() as s:
        a = await _add(s, simhash=base, seen=_ago(hours=2))
        b = await _add(s, simhash=base ^ (1 << 62), seen=_ago(hours=1))
        assert await link_copy(s, b, now=NOW) == a


async def test_old_near_ignored_but_old_exact_linked(db: Database) -> None:
    h = b"\x02" * 32
    base = 0x00FF00FF00FF00FF
    async with db.transaction() as s:
        old_near = await _add(s, simhash=base, seen=_ago(days=40))
        old_exact = await _add(s, simhash=2**33, content_hash=h, seen=_ago(days=50))
        near = await _add(s, simhash=base ^ 1, seen=_ago(hours=1))
        exact = await _add(s, simhash=2**55, content_hash=h, seen=_ago(hours=1))
        assert old_near != old_exact
        assert await link_copy(s, near, now=NOW) is None
        assert await link_copy(s, exact, now=NOW) == old_exact


async def test_root_resolution(db: Database) -> None:
    base = 0x1234
    async with db.transaction() as s:
        a = await _add(s, simhash=base, seen=_ago(hours=3))
        b = await _add(s, simhash=base ^ 1, seen=_ago(hours=2))
        c = await _add(s, simhash=base ^ 2, seen=_ago(hours=1))
        assert await link_copy(s, b, now=NOW) == a
        assert await link_copy(s, c, now=NOW) == a


async def test_root_resolution_through_copy(db: Database) -> None:
    # B is the oldest candidate of C (A is out of the near window) and B copies A.
    base = 0x4321
    async with db.transaction() as s:
        a = await _add(s, simhash=base, seen=_ago(days=31))
        b = await _add(s, simhash=base ^ 1, seen=_ago(days=20), copy_of=a)
        c = await _add(s, simhash=base ^ 2, seen=_ago(hours=1))
        assert await link_copy(s, c, now=NOW) == a
        assert b != c


async def test_never_links_to_younger(db: Database) -> None:
    h = b"\x03" * 32
    async with db.transaction() as s:
        old = await _add(s, simhash=7, content_hash=h, seen=_ago(hours=2))
        young = await _add(s, simhash=7, content_hash=h, seen=_ago(hours=1))
        assert await link_copy(s, old, now=NOW) is None
        assert await link_copy(s, young, now=NOW) == old
        same = await _add(s, simhash=0x5555AAAA5555AAAA, seen=young_ts(NOW))
        other = await _add(s, simhash=0x5555AAAA5555AAAA, seen=young_ts(NOW))
        assert await link_copy(s, same, now=NOW) is None
        assert await link_copy(s, other, now=NOW) == same


def young_ts(t: datetime) -> datetime:
    return t - timedelta(minutes=1)


async def test_idempotent_and_missing(db: Database) -> None:
    h = b"\x04" * 32
    async with db.transaction() as s:
        a = await _add(s, content_hash=h, seen=_ago(hours=2))
        b = await _add(s, content_hash=h, seen=_ago(hours=1))
        assert await link_copy(s, 999_999, now=NOW) is None
        assert await link_copy(s, b, now=NOW) == a
        assert await link_copy(s, b, now=NOW) is None
        assert await _copy_of(s, b) == a


def _ctx(db: Database, settings: Settings, payload: dict[str, Any]) -> JobContext:
    job = JobRecord(
        id=1,
        queue=QUEUE,
        kind="ingest.link_copies",
        payload=payload,
        priority=0,
        attempts=1,
        max_attempts=3,
        dedupe_key=None,
        created_at=NOW,
    )
    return JobContext(job=job, db=db, settings=settings, log=structlog.get_logger("test"))


async def test_link_copies_handler(db: Database, settings: Settings) -> None:
    h = b"\x05" * 32
    async with db.transaction() as s:
        a = await _add(s, content_hash=h, seen=_ago(hours=2))
        b = await _add(s, content_hash=h, seen=_ago(hours=1))
    await link_copies(_ctx(db, settings, {"content_id": b}))
    await link_copies(_ctx(db, settings, {"content_id": b}))
    await link_copies(_ctx(db, settings, {"content_id": 424242}))
    async with db.session() as s:
        assert await _copy_of(s, b) == a
        assert await _copy_of(s, a) is None


# --- partitions -----------------------------------------------------------


def _monday(t: datetime) -> date:
    d = t.date()
    return d - timedelta(days=d.weekday())


async def _partitions(s: AsyncSession) -> set[str]:
    rows = await s.execute(
        text(
            "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
            "WHERE i.inhparent = 'observations'::regclass"
        )
    )
    return {r[0] for r in rows}


async def test_ensure_partitions(db: Database) -> None:
    monday = _monday(NOW)
    wanted = {f"observations_p{monday + timedelta(days=7 * i):%Y%m%d}" for i in range(3)}
    async with db.transaction() as s:
        await ensure_partitions(s, NOW, weeks_ahead=2)
        assert wanted <= await _partitions(s)
        assert await ensure_partitions(s, NOW, weeks_ahead=2) == 0


@asynccontextmanager
async def _old_partition(db: Database, weeks_ago: int) -> AsyncIterator[str]:
    week = _monday(NOW) - timedelta(days=7 * weeks_ago)
    name = f"observations_p{week:%Y%m%d}"
    async with db.transaction() as s:
        await s.execute(
            text(
                f'CREATE TABLE IF NOT EXISTS "{name}" PARTITION OF observations '
                f"FOR VALUES FROM ('{week} 00:00+00') TO ('{week + timedelta(days=7)} 00:00+00')"
            )
        )
    try:
        yield name
    finally:
        async with db.transaction() as s:
            await s.execute(text(f'DROP TABLE IF EXISTS "{name}"'))


async def test_purge_aggregates_and_drops(db: Database) -> None:
    week = _monday(NOW) - timedelta(days=7 * 8)
    t0 = datetime(week.year, week.month, week.day, 10, 5, tzinfo=UTC)
    async with _old_partition(db, 8) as name:
        async with db.transaction() as s:
            c1 = await _add(s)
            c2 = await _add(s)
            for cid, ts in [
                (c1, t0),
                (c1, t0 + timedelta(minutes=20)),
                (c2, t0 + timedelta(hours=1)),
            ]:
                await s.execute(
                    text(
                        "INSERT INTO observations (observed_at, content_id, platform, source) "
                        "VALUES (:t, :c, 'x', 'extension')"
                    ),
                    {"t": ts, "c": cid},
                )
        async with db.transaction() as s:
            assert await purge_partitions(s, NOW, retention_days=30) == 2
            assert name not in await _partitions(s)
            rows = (
                await s.execute(
                    text("SELECT content_id, hour, count FROM observation_counters ORDER BY 1, 2")
                )
            ).all()
            assert [(r[0], r[1], r[2]) for r in rows] == [
                (c1, t0.replace(minute=0), 2),
                (c2, (t0 + timedelta(hours=1)).replace(minute=0), 1),
            ]
            for i in range(3):
                recent = f"observations_p{_monday(NOW) + timedelta(days=7 * i):%Y%m%d}"
                await ensure_partitions(s, NOW)
                assert recent in await _partitions(s)
            assert await purge_partitions(s, NOW, retention_days=30) == 0
            assert (
                await s.execute(text("SELECT count(*) FROM observation_counters"))
            ).scalar_one() == 2


async def test_purge_keeps_partition_not_fully_expired(db: Database) -> None:
    # Week ended ~20 days ago: kept. Week that ended 31 days ago: dropped.
    async with (
        _old_partition(db, 3) as kept,
        _old_partition(db, 6) as dropped,
        db.transaction() as s,
    ):
        await purge_partitions(s, NOW, retention_days=30)
        parts = await _partitions(s)
        assert kept in parts
        assert dropped not in parts


async def test_maintain_partitions_wrapper(db: Database, settings: Settings) -> None:
    async with db.transaction() as s:
        assert await maintain_partitions(s, settings) >= 0


# --- secrets ----------------------------------------------------------------


async def test_rotate_secrets(db: Database) -> None:
    today = NOW.date()
    token = "A" * 22
    async with db.transaction() as s:
        old = await daily_secret(s, today - timedelta(days=1))
        await daily_secret(s, today - timedelta(days=3))
        for day in (today - timedelta(days=31), today - timedelta(days=29)):
            await s.execute(
                text(
                    "INSERT INTO installation_daily (day, pseudonym, items, quota) "
                    "VALUES (:d, :p, 0, 10)"
                ),
                {"d": day, "p": pysecrets.token_bytes(16)},
            )
        stale_pseudonym = installation_pseudonym(old, token)
        assert await rotate(s, today) == 3
        days = {r[0] for r in await s.execute(text("SELECT day FROM installation_secrets"))}
        assert days == {today, today + timedelta(days=1)}
        remaining = (await s.execute(text("SELECT count(*) FROM installation_daily"))).scalar_one()
        assert remaining == 1
        p_today = installation_pseudonym(await daily_secret(s, today), token)
        p_tomorrow = installation_pseudonym(await daily_secret(s, today + timedelta(days=1)), token)
        assert p_today != p_tomorrow
        # Yesterday's secret is gone: a fresh one is generated, so it cannot match.
        regenerated = await daily_secret(s, today - timedelta(days=1))
        assert regenerated != old
        assert installation_pseudonym(regenerated, token) != stale_pseudonym
        assert await rotate(s, today) == 1  # only the regenerated row
        assert await rotate(s, today) == 0


# --- service wiring ---------------------------------------------------------


async def test_service_declaration() -> None:
    services = load_services(["ping", "ingest"])
    services.validate(lease_seconds=120)
    assert {t.name: t.every_seconds for t in services.periodic} == {
        "ingest:partitions": 3600,
        "ingest:secrets": 600,
    }
    assert QUEUE in services.queues
    assert "ingest.link_copies" in services.handlers
    assert service.name == "ingest"


async def test_periodic_wrappers_under_singleton(db: Database, settings: Settings) -> None:
    for task in service.periodic:

        async def run(s: AsyncSession, fn: Any = task.fn) -> int:
            return int(await fn(s, settings))

        assert await run_singleton(db, task.name, run) is not None
