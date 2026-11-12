"""Verdict reuse in ``record_extraction``: identical signature (a joined claim is covered by
the group verdict), N4 and expiry rules, running work, band inheritance through validated
reference facts, and the account override trigger of ``on_content_ingested``."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.factcheck import names
from tower.core.models.factcheck_v1 import LevelOverride, ReferenceFact, VerdictBand
from tower.services.factcheck.jobs import on_content_ingested

from .test_factcheck_matching import (
    Harness,
    _reset,  # noqa: F401  (autouse fixture)
    events_of,
    give_verdict,
    group_rows,
    stat,
    vec,
)
from .test_factcheck_service import (
    claim,
    event,
    jobs_of,
    make_content,
    rows,
    scalar,
    signature,
)


@pytest.fixture
def h(db: Database, settings: Settings) -> Harness:
    return Harness(db, settings)


async def finish_investigations(db: Database) -> None:
    async with db.transaction() as session:
        await session.execute(text("UPDATE investigations SET stage = 'assembled'"))


A = "Le taux de chômage est de 7 % en France."
B = "Le chômage atteint 7 % en France."
FUTURE = datetime.now(UTC) + timedelta(days=30)


def twins(h: Harness) -> None:
    """A and B are the same claim for E3 (identical signature, cosine 0.97)."""
    h.embedder.table = {A: vec(1.0), B: vec(0.97)}


# ------------------------------------------------------------ identical signature


async def test_joined_claim_is_covered_by_the_active_verdict(db: Database, h: Harness) -> None:
    twins(h)
    await h.post(stat(A))
    verdict_id = await give_verdict(db, expires_at=FUTURE)
    cid = await h.post(stat(B))

    reused = await events_of(db, names.VERDICT_REUSED)
    assert len(reused) == 1
    payload = reused[0]
    public = str(await scalar(db, "SELECT public_id FROM verdicts WHERE id = :i", i=verdict_id))
    assert payload["verdict_id"] == public and payload["origin_verdict_id"] == public
    assert payload["rule"] == "identical_signature" and payload["content_id"] == str(cid)
    assert payload["group_id"] == str(await scalar(db, "SELECT public_id FROM claim_groups"))
    assert await scalar(db, "SELECT count(*) FROM investigations") == 1  # no new investigation
    assert len(await group_rows(db)) == 1


async def test_expired_verdict_is_not_reused(db: Database, h: Harness) -> None:
    twins(h)
    await h.post(stat(A))
    await finish_investigations(db)
    await give_verdict(db, expires_at=datetime.now(UTC) - timedelta(minutes=1))
    await h.post(stat(B))
    assert await events_of(db, names.VERDICT_REUSED) == []
    assert await scalar(db, "SELECT count(*) FROM investigations") == 2


async def test_non_active_verdict_is_not_reused(db: Database, h: Harness) -> None:
    twins(h)
    await h.post(stat(A))
    await finish_investigations(db)
    await give_verdict(db, status="rejected")
    await h.post(stat(B))
    assert await events_of(db, names.VERDICT_REUSED) == []
    assert await scalar(db, "SELECT count(*) FROM investigations") == 2


async def test_n4_is_never_directly_reused(db: Database, h: Harness) -> None:
    twins(h)
    await h.post(stat(A))
    await give_verdict(db, expires_at=FUTURE)
    await h.post(stat(B), level="N4")

    assert await events_of(db, names.VERDICT_REUSED) == []
    levels = await rows(db, "SELECT level FROM investigations ORDER BY id")
    assert [r[0] for r in levels] == ["N3", "N4"]  # an N4 investigation is always opened
    assert (await group_rows(db))[0][2] == "N4"
    changed = await events_of(db, names.CLAIM_GROUP_LEVEL_CHANGED)
    assert changed[0]["from_level"] == "N3" and changed[0]["to_level"] == "N4"
    assert changed[0]["reason"] == "content"
    journal = await rows(db, "SELECT kind FROM claim_group_events ORDER BY id")
    assert [r[0] for r in journal] == ["created", "joined", "level_changed"]


async def test_running_investigation_prevents_a_second_one(db: Database, h: Harness) -> None:
    twins(h)
    await h.post(stat(A))
    await h.post(stat(B))
    assert await scalar(db, "SELECT count(*) FROM investigations") == 1
    assert await events_of(db, names.VERDICT_REUSED) == []  # no verdict yet: nothing to reuse


async def test_failed_investigation_is_requested_again_on_join(db: Database, h: Harness) -> None:
    twins(h)
    await h.post(stat(A))
    async with db.transaction() as session:
        await session.execute(text("UPDATE investigations SET stage = 'failed'"))
    await h.post(stat(B))
    assert await scalar(db, "SELECT count(*) FROM investigations") == 2


async def test_draft_verdict_waiting_for_review_covers_the_claim(db: Database, h: Harness) -> None:
    twins(h)
    await h.post(stat(A))
    async with db.transaction() as session:
        await session.execute(text("UPDATE investigations SET stage = 'completed'"))
    await give_verdict(db, status="draft")
    await h.post(stat(B))
    assert await scalar(db, "SELECT count(*) FROM investigations") == 1
    assert await events_of(db, names.VERDICT_REUSED) == []


# ---------------------------------------------------------------- band inheritance

BAND_CLAIM = "Le chômage atteint 7,5 % en France."


async def seed_fact(
    db: Database,
    *,
    status: str = "validated",
    value: str = "7.4",
    previous: int | None = None,
    next_publication: date | None = None,
    published: date = date(2026, 1, 1),
) -> int:
    async with db.transaction() as session:
        fact = ReferenceFact(
            public_id=uuid.uuid4(),
            indicator="fr.unemployment_rate",
            label="Taux de chômage",
            value=value,
            unit="percent",
            period_start=published,
            period_end=published,
            territory="FR",
            source_url="https://www.insee.fr/x",
            source_name="INSEE",
            published_at=published,
            next_publication=next_publication,
            previous_fact_id=previous,
            status=status,
            origin="manual",
            created_by="staff:1",
        )
        session.add(fact)
        await session.flush()
        return int(fact.id)


async def seed_band(
    db: Database,
    fact_id: int,
    source_verdict_id: int | None,
    *,
    band: str = "within_1_tau",
    side: str = "above",
    status: str = "active",
) -> int:
    async with db.transaction() as session:
        row = VerdictBand(
            public_id=uuid.uuid4(),
            reference_fact_id=fact_id,
            band=band,
            side=side,
            veracity="misleading",
            context_need="useful",
            context_types=["outdated"],
            source_verdict_id=source_verdict_id,
            status=status,
            validated_by="staff:2",
            revoked_by="staff:3" if status == "revoked" else None,
        )
        session.add(row)
        await session.flush()
        return int(row.id)


async def source_verdict(db: Database, h: Harness, **kw: Any) -> int:
    """A group of another claim (7 %, other subject) holding the band's source verdict."""
    other = stat("Le chômage des jeunes est de 17 % en France.", value=17.0, subject="jeunes")
    await h.post(other)
    return await give_verdict(db, **{"expires_at": FUTURE, "veracity": "misleading", **kw})


async def test_new_group_inherits_the_verdict_of_a_validated_band(db: Database, h: Harness) -> None:
    source = await source_verdict(db, h)
    fact = await seed_fact(db)
    await seed_band(db, fact, source)
    investigations_before = await scalar(db, "SELECT count(*) FROM investigations")
    await h.post(stat(BAND_CLAIM, value=7.5, value_low=7.45, value_high=7.55))

    groups = await rows(
        db,
        "SELECT state, active_verdict_id, reference_fact_id, band, band_side FROM claim_groups"
        " ORDER BY id DESC LIMIT 1",
    )
    state, verdict_id, fact_id, band, side = groups[0]
    assert (state, fact_id, band, side) == ("passive", fact, "within_1_tau", "above")
    verdict = (
        await rows(
            db,
            "SELECT veracity, status, reuse_rule, reused_from_verdict_id, validated_by,"
            " drafted_at, expires_at FROM verdicts WHERE id = :i",
            i=verdict_id,
        )
    )[0]
    assert verdict[0] == "misleading" and verdict[1] == "active" and verdict[2] == "band"
    assert verdict[3] == source and verdict[4] == "system:factcheck"
    assert verdict[5] is None  # never counted in the review quota
    assert verdict[6] is not None
    assert await scalar(db, "SELECT count(*) FROM investigations") == investigations_before
    reused = (await events_of(db, names.VERDICT_REUSED))[-1]
    assert reused["rule"] == "band"
    assert reused["origin_verdict_id"] == str(
        await scalar(db, "SELECT public_id FROM verdicts WHERE id = :i", i=source)
    )
    created = (await events_of(db, names.CLAIM_GROUP_CREATED))[-1]
    assert created["band"] == "within_1_tau" and created["band_side"] == "above"
    assert created["reference_fact_id"] == str(
        await scalar(db, "SELECT public_id FROM reference_facts")
    )
    kinds = [r[0] for r in await rows(db, "SELECT kind FROM claim_group_events ORDER BY id")]
    assert kinds[-2:] == ["created", "fact_linked"]


async def test_band_verdict_expires_with_the_next_publication(db: Database, h: Harness) -> None:
    source = await source_verdict(db, h, expires_at=datetime.now(UTC) + timedelta(days=100))
    next_pub = date.today() + timedelta(days=10)
    fact = await seed_fact(db, next_publication=next_pub)
    await seed_band(db, fact, source)
    await h.post(stat(BAND_CLAIM, value=7.5, value_low=7.45, value_high=7.55))
    expires = await scalar(db, "SELECT expires_at FROM verdicts WHERE reuse_rule = 'band'")
    assert expires.date() == next_pub


@pytest.mark.parametrize("case", ["pending_fact", "revoked_band", "no_band", "source_superseded"])
async def test_without_a_valid_band_the_normal_path_applies(
    db: Database, h: Harness, case: str
) -> None:
    source = await source_verdict(
        db, h, status="superseded" if case == "source_superseded" else "active"
    )
    fact = await seed_fact(db, status="pending" if case == "pending_fact" else "validated")
    if case != "no_band":
        await seed_band(db, fact, source, status="revoked" if case == "revoked_band" else "active")
    await h.post(stat(BAND_CLAIM, value=7.5, value_low=7.45, value_high=7.55))

    assert await scalar(db, "SELECT count(*) FROM verdicts WHERE reuse_rule = 'band'") == 0
    last = (
        await rows(
            db,
            "SELECT i.flags, g.state FROM investigations i JOIN claim_groups g"
            " ON g.id = i.claim_group_id ORDER BY i.id DESC LIMIT 1",
        )
    )[0]
    assert last[1] == "queued"
    if case == "pending_fact":
        assert "reference_fact_id" not in last[0]  # pending facts never serve
    else:
        assert last[0]["reference_fact_id"]  # validated fact: linked, investigation as usual
    assert await events_of(db, names.VERDICT_REUSED) == []


async def test_n4_never_inherits_a_band_verdict(db: Database, h: Harness) -> None:
    source = await source_verdict(db, h)
    fact = await seed_fact(db)
    await seed_band(db, fact, source)
    await h.post(stat(BAND_CLAIM, value=7.5, value_low=7.45, value_high=7.55), level="N4")
    assert await scalar(db, "SELECT count(*) FROM verdicts WHERE reuse_rule = 'band'") == 0
    assert (await scalar(db, "SELECT count(*) FROM investigations WHERE level = 'N4'")) == 1


async def test_outdated_claim_is_flagged_on_its_investigation(db: Database, h: Harness) -> None:
    previous = await seed_fact(db, value="7.0", published=date(2025, 10, 1))
    await seed_fact(db, value="7.4", previous=previous)
    await h.post(
        stat("Le chômage est de 7 % en France.", value=7.0, value_low=6.95, value_high=7.05)
    )
    flags = (await rows(db, "SELECT flags FROM investigations ORDER BY id DESC LIMIT 1"))[0][0]
    assert flags["outdated"] is True and flags["reference_fact_id"]


async def test_non_numeric_claims_resolve_no_fact(db: Database, h: Harness) -> None:
    await seed_fact(db)
    await h.post(claim(A, sig=signature(subject="taux de chômage", unit="%")))
    assert (await group_rows(db))[0][0] is not None
    assert await scalar(db, "SELECT reference_fact_id FROM claim_groups") is None


async def test_external_factchecks_reach_the_investigation_flags(db: Database, h: Harness) -> None:
    from tower.core.adapters.knowledge.atlas import AtlasDocument, AtlasResult
    from tower.core.factcheck import runtime

    class FakeAtlas:
        async def collect(self, request: Any) -> AtlasResult:
            doc = AtlasDocument(
                adapter="factcheck_tools",
                canonical_url="https://checker.example/a",
                original_url="https://checker.example/a?utm=1",
                domain="checker.example",
                tier="secondary",
                lang="fr",
                text="Chômage à 7 % — Checker: Faux (2026-01-02)",
                title="Non, le chômage n'est pas à 7 %",
                publisher="Checker",
            )
            return AtlasResult(documents=[doc, doc, doc, doc], adapters_used=["factcheck_tools"])

    runtime.set_override("atlas", FakeAtlas())
    await h.post(stat(A))
    flags = (await rows(db, "SELECT flags FROM investigations"))[0][0]
    checks = flags["external_factchecks"]
    assert len(checks) == 3  # at most three
    assert checks[0] == {
        "url": "https://checker.example/a?utm=1",
        "publisher": "Checker",
        "rating": "Faux",
        "title": "Non, le chômage n'est pas à 7 %",
    }


async def test_external_lookup_failure_is_never_fatal(db: Database, h: Harness) -> None:
    from tower.core.factcheck import runtime

    class BrokenAtlas:
        async def collect(self, request: Any) -> Any:
            raise RuntimeError("boom")

    runtime.set_override("atlas", BrokenAtlas())
    await h.post(stat(A))
    flags = (await rows(db, "SELECT flags FROM investigations"))[0][0]
    assert "external_factchecks" not in flags


async def test_external_lookup_can_be_disabled(db: Database, settings: Settings) -> None:
    from tower.core.factcheck import runtime

    from .conftest import make_settings

    class Boom:
        async def collect(self, request: Any) -> Any:
            raise AssertionError("must not be called")

    runtime.set_override("atlas", Boom())
    local = Harness(db, make_settings(fc_external_factcheck_lookup=False))
    await local.post(stat(A))
    assert await scalar(db, "SELECT count(*) FROM investigations") == 1


# ------------------------------------------------------------ account override trigger


async def _account_of(db: Database, content_id: int) -> int:
    return int(
        await scalar(db, "SELECT author_account_id FROM content_items WHERE id = :i", i=content_id)
    )


async def _override(db: Database, account_id: int, level: str, *, expires: datetime) -> None:
    async with db.transaction() as session:
        session.add(
            LevelOverride(
                public_id=uuid.uuid4(),
                target_type="account",
                target_id=account_id,
                level=level,
                expires_at=expires,
                author="staff:1",
                reason="test",
            )
        )


async def _ingested(db: Database, content_id: int, lang: str = "fr") -> None:
    async with db.transaction() as session:
        await on_content_ingested(
            session, [event("content.ingested", {"id": content_id, "lang": lang})]
        )


@pytest.mark.parametrize("level", ["N3", "N4"])
async def test_account_override_enqueues_extraction_without_watching(
    db: Database, level: str
) -> None:
    cid = await make_content(db, "8001", A)
    await _override(db, await _account_of(db, cid), level, expires=FUTURE)
    await _ingested(db, cid)
    jobs = await jobs_of(db, names.JOB_EXTRACT)
    assert [(j["content_id"], j["level"]) for j in jobs] == [(cid, level)]
    assert jobs[0]["_dedupe"] == names.extract_key(cid, level)


async def test_expired_or_low_level_override_enqueues_nothing(db: Database) -> None:
    expired = await make_content(db, "8002", A, author="6001")
    await _override(
        db, await _account_of(db, expired), "N4", expires=datetime.now(UTC) - timedelta(1)
    )
    low = await make_content(db, "8003", A, author="6002")
    await _override(db, await _account_of(db, low), "N2", expires=FUTURE)
    plain = await make_content(db, "8004", A, author="6003")
    for cid in (expired, low, plain):
        await _ingested(db, cid)
    assert await jobs_of(db, names.JOB_EXTRACT) == []


async def test_n4_override_ignores_the_language_filter_but_n3_does_not(db: Database) -> None:
    n3 = await make_content(db, "8005", A, author="6004", lang="de")
    await _override(db, await _account_of(db, n3), "N3", expires=FUTURE)
    n4 = await make_content(db, "8006", A, author="6005", lang="de")
    await _override(db, await _account_of(db, n4), "N4", expires=FUTURE)
    await _ingested(db, n3, "de")
    await _ingested(db, n4, "de")
    jobs = await jobs_of(db, names.JOB_EXTRACT)
    assert [(j["content_id"], j["level"]) for j in jobs] == [(n4, "N4")]


async def test_watched_account_path_is_unchanged(db: Database) -> None:
    from .test_factcheck_service import watch

    cid = await make_content(db, "8007", A, author="6006")
    await watch(db, "6006")
    await _override(db, await _account_of(db, cid), "N4", expires=FUTURE)
    await _ingested(db, cid)
    jobs = await jobs_of(db, names.JOB_EXTRACT)
    # watched => N3 as before (one job, same dedupe key as lot 3)
    assert [(j["content_id"], j["level"]) for j in jobs] == [(cid, "N3")]


# --------------------------------------------------------------- concurrent creation


async def test_concurrent_extractions_of_the_same_claim_create_one_group(
    db: Database, h: Harness
) -> None:
    """Two extractions decided "new" in parallel (neither saw the other's group): the
    advisory lock and the in-transaction recheck make the second one join."""
    import asyncio

    from tower.core.domain.factcheck.matching import MatchDecision
    from tower.core.domain.security import Actor, ActorKind, Permission
    from tower.core.models.ingestion import ContentItem
    from tower.core.usecases.claims import record_extraction

    actor = Actor(
        kind=ActorKind.SYSTEM,
        id="test",
        permissions=frozenset({Permission.FACTCHECK_REQUEST}),
    )
    first = await make_content(db, "9101", A)
    second = await make_content(db, "9102", A)
    decision = MatchDecision.new(embedding=tuple(vec(1.0)), embedding_model="m")

    async def record(content_id: int) -> None:
        async with db.transaction() as session:
            content = await session.get(ContentItem, content_id)
            assert content is not None
            await record_extraction(
                session,
                actor,
                content=content,
                claims=[stat(A)],
                level="N3",
                requested_by="test",
                prompt_version="v",
                thread_context=None,
                matches=[decision],
                post_date=date(2026, 11, 11),
            )
            await asyncio.sleep(0.3)  # keep the lock while the other one waits

    await asyncio.gather(record(first), record(second))
    assert len(await group_rows(db)) == 1
    assert await scalar(db, "SELECT count(*) FROM claim_group_members") == 2
    rule = await scalar(db, "SELECT data->>'rule' FROM claim_group_events WHERE kind = 'joined'")
    assert rule == "identical_signature:recheck"
    assert await scalar(db, "SELECT count(*) FROM investigations") == 1
