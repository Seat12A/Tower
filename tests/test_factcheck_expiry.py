"""Verdict expiry and revalidation (lot 4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import structlog
from sqlalchemy import select

from tests.test_drafts_usecases import Built
from tests.test_factcheck_references import (
    EDITOR,
    Q2,
    active_draft,
    new_fact,
    prepare_group,
)
from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain.factcheck.values import Band, BandSide, ContextNeed, Veracity
from tower.core.domain.security import Actor
from tower.core.factcheck import names
from tower.core.jobs.queue import JobRecord
from tower.core.models.events import Event
from tower.core.models.factcheck import ClaimGroup, ContextNote, Investigation, Verdict
from tower.core.models.factcheck_v1 import ReferenceFact
from tower.core.models.jobs import Job
from tower.core.services.base import JobContext
from tower.core.usecases import expiry as uc
from tower.core.usecases import references as refs
from tower.services.factcheck import revalidation

SYSTEM = Actor.system(names.FACTCHECK_ACTOR)
NOW = datetime.now(UTC)


def ctx(db: Database, settings: Settings, verdict_id: int) -> JobContext:
    job = JobRecord(
        id=1,
        queue=names.FC_BULK,
        kind=names.JOB_REVALIDATE,
        payload={"verdict_id": verdict_id},
        priority=0,
        attempts=1,
        max_attempts=3,
        dedupe_key=None,
        created_at=NOW,
    )
    return JobContext(job=job, db=db, settings=settings, log=structlog.get_logger("test"))


async def set_expiry(db: Database, verdict_id: int, when: datetime | None) -> None:
    async with db.transaction() as s:
        verdict = await s.get(Verdict, verdict_id)
        assert verdict is not None
        verdict.expires_at = when


async def expire(db: Database) -> int:
    async with db.transaction() as s:
        return await uc.expire_verdicts(s, SYSTEM, now=datetime.now(UTC))


async def jobs(db: Database, kind: str) -> list[Job]:
    async with db.session() as s:
        return list((await s.scalars(select(Job).where(Job.kind == kind))).all())


async def expired_group(db: Database, ext: str) -> Built:
    built = await active_draft(db, ext)
    await set_expiry(db, built.verdict_id, datetime.now(UTC) - timedelta(minutes=1))
    assert await expire(db) == 1
    return built


async def test_expire_verdicts(db: Database) -> None:
    fresh = await active_draft(db, "1850000000000000201")
    await set_expiry(db, fresh.verdict_id, datetime.now(UTC) + timedelta(days=1))
    built = await active_draft(db, "1850000000000000202")
    await set_expiry(db, built.verdict_id, datetime.now(UTC) - timedelta(minutes=1))
    assert await expire(db) == 1
    assert await expire(db) == 0  # nothing left: idempotent
    async with db.session() as s:
        verdict = await s.get(Verdict, built.verdict_id)
        group = await s.get(ClaimGroup, built.group_id)
        note = await s.get(ContextNote, built.note_id)
        kept = await s.get(Verdict, fresh.verdict_id)
        assert verdict is not None and group is not None and note is not None and kept is not None
        assert verdict.status == "expired" and kept.status == "active"
        assert group.active_verdict_id is None and group.state == "to_revalidate"
        assert note.status == "expired"
        events = list(
            (await s.scalars(select(Event).where(Event.topic == "verdict.expired"))).all()
        )
    (event,) = events
    assert event.payload["verdict_id"] == str(verdict.public_id)
    assert event.payload["claim_type"] == "statistic"
    (job,) = await jobs(db, names.JOB_REVALIDATE)
    assert job.queue == names.FC_BULK and job.payload == {"verdict_id": built.verdict_id}
    assert job.dedupe_key == names.revalidate_key(built.verdict_id)


async def test_expired_band_inherited_verdict_goes_back_to_queued(db: Database) -> None:
    built = await active_draft(db, "1850000000000000203")
    async with db.transaction() as s:
        verdict = await s.get(Verdict, built.verdict_id)
        assert verdict is not None
        verdict.reuse_rule = "band"
        verdict.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    assert await expire(db) == 1
    async with db.session() as s:
        group = await s.get(ClaimGroup, built.group_id)
        assert group is not None and group.state == "queued" and group.active_verdict_id is None


async def test_expire_provisional_notes(db: Database) -> None:
    built = await active_draft(db, "1850000000000000204")
    async with db.transaction() as s:
        note = await s.get(ContextNote, built.note_id)
        assert note is not None
        note.regime = "provisional"
        note.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    async with db.transaction() as s:
        assert await uc.expire_provisional_notes(s, SYSTEM, now=datetime.now(UTC)) == 1
        assert await uc.expire_provisional_notes(s, SYSTEM, now=datetime.now(UTC)) == 0
        note = await s.get(ContextNote, built.note_id)
        assert note is not None and note.status == "expired"


async def test_periodic_task_runs_both(db: Database, settings: Settings) -> None:
    built = await active_draft(db, "1850000000000000205")
    await set_expiry(db, built.verdict_id, datetime.now(UTC) - timedelta(minutes=1))
    async with db.transaction() as s:
        assert await revalidation.expire(s, settings) == 1


# --- revalidation job ---------------------------------------------------------------


async def test_revalidate_opens_an_investigation_once(db: Database, settings: Settings) -> None:
    built = await expired_group(db, "1850000000000000211")
    await revalidation.revalidate(ctx(db, settings, built.verdict_id))
    await revalidation.revalidate(ctx(db, settings, built.verdict_id))  # replay: no-op
    async with db.session() as s:
        invs = list(
            (
                await s.scalars(
                    select(Investigation).where(Investigation.claim_group_id == built.group_id)
                )
            ).all()
        )
    fresh = [i for i in invs if i.flags.get("revalidation")]
    assert len(fresh) == 1 and fresh[0].level == "N3" and fresh[0].stage == "framing"
    assert fresh[0].flags["previous_verdict_id"] == built.verdict_id


async def test_revalidate_nothing_to_do(db: Database, settings: Settings) -> None:
    await revalidation.revalidate(ctx(db, settings, 999_999))  # unknown verdict
    built = await active_draft(db, "1850000000000000212")
    await revalidation.revalidate(ctx(db, settings, built.verdict_id))  # active verdict
    async with db.session() as s:
        count = len(list((await s.scalars(select(Investigation))).all()))
    assert count == 1  # only the investigation of the draft


async def covered_by_band(db: Database, built: Built, *, with_band: bool) -> Verdict:
    """Group linked to a validated Q1 fact; a newer validated Q2 fact exists, with an
    active band `contains` when ``with_band``. The group claim is [8, 9[."""
    old = await new_fact(db, value=Decimal("7.3"))
    new = await new_fact(db, value=Decimal("8.5"), period_start=Q2[0], period_end=Q2[1])
    assert old.id != new.id
    await prepare_group(db, built, low="8", high="9", fact=old)
    if with_band:
        source = await active_draft(db, "1850000000000000299", low="8", high="9", fact=new)
        async with db.transaction() as s:
            await refs.validate_band(
                s,
                EDITOR,
                fact_id=new.public_id,
                band=Band.CONTAINS,
                side=BandSide.NONE,
                veracity=Veracity.TRUE,
                context_need=ContextNeed.NONE,
                source_verdict_id=source.verdict,
            )
    async with db.session() as s:
        verdict = await s.get(Verdict, built.verdict_id)
        assert verdict is not None
        return verdict


async def test_revalidate_inherits_through_the_band_of_a_newer_fact(
    db: Database, settings: Settings
) -> None:
    built = await active_draft(db, "1850000000000000221")
    await covered_by_band(db, built, with_band=True)
    await set_expiry(db, built.verdict_id, datetime.now(UTC) - timedelta(minutes=1))
    assert await expire(db) == 1
    await revalidation.revalidate(ctx(db, settings, built.verdict_id))
    await revalidation.revalidate(ctx(db, settings, built.verdict_id))
    async with db.session() as s:
        group = await s.get(ClaimGroup, built.group_id)
        assert group is not None and group.state == "passive"
        assert group.band == "contains" and group.band_side == "none"
        active = (
            await s.scalars(
                select(Verdict).where(
                    Verdict.claim_group_id == built.group_id, Verdict.status == "active"
                )
            )
        ).all()
        assert len(active) == 1 and active[0].reuse_rule == "band"
        assert active[0].veracity == "true" and group.active_verdict_id == active[0].id
        reused = list((await s.scalars(select(Event).where(Event.topic == "verdict.reused"))).all())
        invs = list((await s.scalars(select(Investigation))).all())
    assert len(reused) == 1
    assert not [i for i in invs if i.flags.get("revalidation")]  # no new investigation


async def test_revalidate_without_band_investigates_on_the_newer_fact(
    db: Database, settings: Settings
) -> None:
    built = await active_draft(db, "1850000000000000222")
    await covered_by_band(db, built, with_band=False)
    await set_expiry(db, built.verdict_id, datetime.now(UTC) - timedelta(minutes=1))
    assert await expire(db) == 1
    await revalidation.revalidate(ctx(db, settings, built.verdict_id))
    async with db.session() as s:
        group = await s.get(ClaimGroup, built.group_id)
        assert group is not None and group.band == "contains"  # relinked to Q2 (8.5)
        assert group.reference_fact_id is not None
        linked = await s.get(ReferenceFact, group.reference_fact_id)
        assert linked is not None and linked.period_end == Q2[1]
        fresh = [
            i for i in (await s.scalars(select(Investigation))).all() if i.flags.get("revalidation")
        ]
    assert len(fresh) == 1


async def test_revalidate_n4_never_inherits(db: Database, settings: Settings) -> None:
    built = await active_draft(db, "1850000000000000223")
    await covered_by_band(db, built, with_band=True)
    async with db.transaction() as s:
        group = await s.get(ClaimGroup, built.group_id)
        assert group is not None
        group.level = "N4"
    await set_expiry(db, built.verdict_id, datetime.now(UTC) - timedelta(minutes=1))
    await expire(db)
    await revalidation.revalidate(ctx(db, settings, built.verdict_id))
    async with db.session() as s:
        fresh = [
            i for i in (await s.scalars(select(Investigation))).all() if i.flags.get("revalidation")
        ]
        group = await s.get(ClaimGroup, built.group_id)
    assert len(fresh) == 1 and fresh[0].level == "N4"
    assert group is not None and group.active_verdict_id is None
