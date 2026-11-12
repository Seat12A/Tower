"""Investigate jobs against a real PostgreSQL: full chain, idempotent replay, routing,
retention, refusals, budget and failure handling. Model and Atlas are fakes; no network."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import structlog
from sqlalchemy import text

from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain.factcheck.values import Veracity
from tower.core.errors import get_reporter
from tower.core.events.outbox import EventRecord
from tower.core.factcheck import names, runtime
from tower.core.jobs.queue import JobRecord, PermanentError, RetryLater
from tower.core.llm.errors import (
    AllRefused,
    BudgetExhausted,
    NoValidOutput,
    ProviderNotConfigured,
)
from tower.core.models.factcheck import Claim, ClaimGroup, Investigation
from tower.core.models.ingestion import ContentItem, Entity, PlatformAccount, WatchedAccount
from tower.core.services.base import JobContext
from tower.core.services.registry import ServiceSet, load_service
from tower.services.investigate import service
from tower.services.investigate.jobs import on_requested

from .conftest import make_settings
from .test_investigate_pipeline import (
    POST_DATE,
    FakeAtlas,
    FakeLLM,
    default_docs,
    extra_docs,
    frame_output,
    judge_output,
    second_opinion,
)

HANDLERS = {spec.kind: spec.handler for spec in service.jobs}
CLAIM_TEXT = "Unemployment fell to 7 percent in 2025"


@pytest.fixture(autouse=True)
async def _overrides() -> AsyncIterator[None]:
    yield
    runtime.clear_overrides()


def install(
    llm: FakeLLM | None = None, atlas: FakeAtlas | None = None
) -> tuple[FakeLLM, FakeAtlas]:
    llm = llm or FakeLLM()
    atlas = atlas or FakeAtlas()
    runtime.set_override("llm", llm)
    runtime.set_override("atlas", atlas)
    return llm, atlas


async def seed(
    db: Database,
    *,
    level: str = "N3",
    lang: str = "fr",
    author_handle: str | None = None,
    author_name: str | None = None,
    watched: bool = False,
    post_text: str = "Le chomage a baisse selon une source officielle",
) -> int:
    """A claim, its group, the post and an investigation at stage ``framing``."""
    async with db.transaction() as session:
        account_id: int | None = None
        if author_handle is not None:
            entity = Entity(name=author_name or author_handle, kind="person")
            session.add(entity)
            await session.flush()
            account = PlatformAccount(
                platform="x", external_id="4242", handle=author_handle, entity_id=entity.id
            )
            session.add(account)
            await session.flush()
            account_id = account.id
            if watched:
                session.add(
                    WatchedAccount(
                        account_id=account.id,
                        category="minister",
                        priority=50,
                        added_by="test",
                    )
                )
        content = ContentItem(
            platform="x",
            external_id=str(9000 + abs(hash(uuid.uuid4())) % 10**6),
            author_account_id=account_id,
            text=post_text,
            lang=lang,
            lang_confidence=0.95,
            useful_words=9,
            content_hash=b"\x00" * 32,
            simhash=0,
            metrics={"likes": 987654, "reposts": 123456, "impressions": 5555555},
            created_at_platform=POST_DATE,
        )
        claim = Claim(
            lang_original="en",
            text_original=CLAIM_TEXT,
            text_fr="Le chomage a baisse a 7 % en 2025",
            claim_type="statistic",
            jurisdictions=["FR"],
            primary_jurisdiction="FR",
            entities=[{"name": "Insee", "role": "source", "qid": None}],
            verifiability=0.9,
            context_hints=[],
            prompt_version="test",
        )
        session.add_all([content, claim])
        await session.flush()
        group = ClaimGroup(representative_claim_id=claim.id, level=level, state="investigating")
        session.add(group)
        await session.flush()
        inv = Investigation(
            claim_group_id=group.id,
            content_id=content.id,
            level=level,
            depth="reinforced" if level == "N4" else "standard",
            stage="framing",
            requested_by="test",
        )
        session.add(inv)
        await session.flush()
        return inv.id


async def run_job(db: Database, settings: Settings, job: JobRecord) -> None:
    ctx = JobContext(job=job, db=db, settings=settings, log=structlog.get_logger("test"))
    await HANDLERS[job.kind](ctx)


async def drain(
    db: Database,
    settings: Settings,
    *,
    attempts: int = 1,
    max_attempts: int = 5,
    limit: int = 40,
    count: int | None = None,
) -> list[JobRecord]:
    """Run the queued ``fc-deep`` jobs one by one until none is left; return them."""
    done: list[JobRecord] = []
    for _ in range(limit):
        async with db.transaction() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT id, queue, kind, payload, priority, dedupe_key, created_at "
                        "FROM jobs WHERE status = 'queued' AND queue = 'fc-deep' "
                        "ORDER BY id LIMIT 1"
                    )
                )
            ).first()
            if row is None:
                return done
            await session.execute(
                text("UPDATE jobs SET status = 'succeeded' WHERE id = :id"), {"id": row.id}
            )
        job = JobRecord(
            id=row.id,
            queue=row.queue,
            kind=row.kind,
            payload=row.payload,
            priority=row.priority,
            attempts=attempts,
            max_attempts=max_attempts,
            dedupe_key=row.dedupe_key,
            created_at=row.created_at,
        )
        done.append(job)
        await run_job(db, settings, job)
        if count is not None and len(done) >= count:
            return done
    raise AssertionError("job chain did not terminate")


async def requeue(db: Database, kind: str, inv_id: int, round_: int = 1) -> None:
    """Put a job back in the queue (what the runner does after a retry)."""
    async with db.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO jobs (queue, kind, payload) "
                "VALUES ('fc-deep', :kind, CAST(:payload AS jsonb))"
            ),
            {"kind": kind, "payload": f'{{"investigation_id": {inv_id}, "round": {round_}}}'},
        )


async def scalar(db: Database, sql: str, **params: Any) -> Any:
    async with db.session() as session:
        return (await session.execute(text(sql), params)).scalar_one()


async def rows(db: Database, sql: str, **params: Any) -> list[Any]:
    async with db.session() as session:
        return list((await session.execute(text(sql), params)).all())


async def investigation(db: Database, inv_id: int) -> Investigation:
    async with db.session() as session:
        inv = await session.get(Investigation, inv_id)
        assert inv is not None
        return inv


async def completed_events(db: Database) -> list[dict[str, Any]]:
    result = await rows(
        db, "SELECT payload FROM events WHERE topic = 'investigation.completed' ORDER BY id"
    )
    return [r.payload for r in result]


def kinds(jobs: Sequence[JobRecord]) -> list[str]:
    return [j.kind.removeprefix("investigate.") for j in jobs]


# ---------------------------------------------------------------------- service


def test_service_declaration_is_valid() -> None:
    assert {s.kind for s in service.jobs} == {
        names.JOB_FRAME,
        names.JOB_COLLECT,
        names.JOB_JUDGE,
        names.JOB_SECOND_OPINION,
        names.JOB_OBJECTION,
        names.JOB_ESCALATE,
        names.JOB_DRAFT,
    }
    assert {s.queue for s in service.jobs} == {names.FC_DEEP}
    ServiceSet(services=(load_service("investigate"),)).validate(lease_seconds=300)


async def test_subscription_enqueues_frame_job(db: Database) -> None:
    inv_id = await seed(db)
    inv = await investigation(db, inv_id)
    now = datetime.now(UTC)

    def event(raw: object) -> EventRecord:
        return EventRecord(
            id=1,
            xid=1,
            topic=names.INVESTIGATION_REQUESTED,
            aggregate_type="investigation",
            aggregate_id=str(inv.public_id),
            payload={"investigation_id": raw},
            actor="system:factcheck",
            created_at=now,
        )

    async with db.transaction() as session:
        await on_requested(
            session,
            [event(str(inv.public_id)), event(str(inv.public_id)), event(inv_id), event("999999")],
        )
    jobs = await rows(db, "SELECT kind, payload, dedupe_key, queue FROM jobs")
    assert len(jobs) == 1
    assert jobs[0].kind == names.JOB_FRAME and jobs[0].queue == names.FC_DEEP
    assert jobs[0].payload == {"investigation_id": inv_id}
    assert jobs[0].dedupe_key == names.stage_key("frame", inv.claim_group_id, inv_id, 1)


async def test_wave_investigation_jobs_jump_the_deep_queue(db: Database) -> None:
    inv_id = await seed(db)
    async with db.transaction() as session:
        await session.execute(
            text("UPDATE investigations SET flags = '{\"wave\": true}' WHERE id = :id"),
            {"id": inv_id},
        )
    await _request(db, inv_id)
    jobs = await rows(db, "SELECT kind, priority FROM jobs")
    assert [(j.kind, j.priority) for j in jobs] == [(names.JOB_FRAME, names.FAST_LANE_PRIORITY)]


@pytest.mark.parametrize(("level", "expected"), [("N2", 0), ("N3", 10), ("N4", 10)])
async def test_immediate_levels_take_the_fast_lane(db: Database, level: str, expected: int) -> None:
    inv_id = await seed(db)
    async with db.transaction() as session:
        await session.execute(
            text("UPDATE investigations SET level = :l, flags = '{}' WHERE id = :id"),
            {"l": level, "id": inv_id},
        )
    await _request(db, inv_id)
    jobs = await rows(db, "SELECT priority FROM jobs")
    assert [j.priority for j in jobs] == [expected]


async def _request(db: Database, inv_id: int) -> None:
    inv = await investigation(db, inv_id)
    async with db.transaction() as session:
        await on_requested(
            session,
            [
                EventRecord(
                    id=1,
                    xid=1,
                    topic=names.INVESTIGATION_REQUESTED,
                    aggregate_type=None,
                    aggregate_id=None,
                    payload={"investigation_id": str(inv.public_id)},
                    actor=None,
                    created_at=datetime.now(UTC),
                )
            ],
        )


# ------------------------------------------------------------------- happy path


async def test_full_chain_n3_standard(db: Database, settings: Settings) -> None:
    llm, atlas = install()
    inv_id = await seed(db)
    await _request(db, inv_id)
    jobs = await drain(db, settings)
    assert kinds(jobs) == ["frame", "collect", "judge", "second_opinion", "escalate", "draft"]
    inv = await investigation(db, inv_id)
    assert inv.stage == "completed" and inv.completed_at is not None
    # frame, queries, judge, second opinion, writer, critic and one German translation.
    assert inv.cost_usd_micros == 7000
    assert inv.agreement == "identical" and inv.judgement is not None
    assert inv.second_pass is None and inv.objection is None
    assert inv.queries is not None and len(inv.queries["rounds"]) == 1
    assert len(atlas.requests) == 1

    verdict = (await rows(db, "SELECT * FROM verdicts"))[0]
    assert verdict.status == "draft" and verdict.drafted_at is None
    assert verdict.veracity == "true" and verdict.context_need == "none"
    assert verdict.prompt_versions["judge"] == "pv1" and verdict.route_versions["writer"] == 3
    assert verdict.flags["depth"] == "standard" and verdict.flags["agreement"] == "identical"
    assert verdict.flags["downgraded"] is False and verdict.flags["independent_origins"] == 3
    assert verdict.flags["has_primary"] is True and verdict.flags["checks"]["blocking"] is False
    assert verdict.flags["critic"]["comments"] == "RAS"
    assert "versions" not in verdict.flags
    assert verdict.review_points == ["Vérifier la définition du chômage."]
    assert len(verdict.rationale) == 3
    assert all(c["valid"] for r in verdict.rationale for c in r["citations"])

    citations = await rows(db, "SELECT * FROM citations ORDER BY position")
    assert len(citations) == 3 and all(c.valid for c in citations)
    notes = await rows(db, "SELECT * FROM context_notes")
    assert len(notes) == 1
    assert notes[0].lang == "fr" and notes[0].status == "draft" and notes[0].regime == "formulation"
    assert notes[0].expires_at is None and notes[0].links
    translations = await rows(db, "SELECT lang, origin, fact_sheet FROM context_note_translations")
    assert [(t.lang, t.origin) for t in translations] == [("fr", "machine")]

    # Retention: cited passages keep their text, the others lose it; sources stay.
    passages = await rows(
        db,
        "SELECT p.label, p.text, p.kept, p.origin, p.translation_fr, s.domain "
        "FROM evidence_passages p JOIN sources s ON s.id = p.source_id ORDER BY p.id",
    )
    assert len(passages) == 4
    for p in passages:
        if p.domain == "uncited.example":
            assert p.text is None and p.kept is False
        else:
            assert p.text and p.kept is True
        assert p.origin == p.domain  # every origin the judge listed is stored
    german = next(p for p in passages if p.domain == "quelle.de")
    assert german.translation_fr == "Texte traduit."
    assert await scalar(db, "SELECT count(*) FROM sources") == 4
    primary_labels = verdict.flags["primary_passages"]
    assert next(p.label for p in passages if p.domain == "insee.fr") in primary_labels

    events = await completed_events(db)
    assert len(events) == 1
    group = await rows(db, "SELECT public_id FROM claim_groups")
    assert events[0] == {
        "investigation_id": str(inv.public_id),
        "group_id": str(group[0].public_id),
        "level": "N3",
        "depth": "standard",
        "verdict_id": str(verdict.public_id),
        "veracity": "true",
        "context_need": "none",
        "outcome": "drafted",
    }
    # Model calls carry the level scope and the role's prompt id.
    assert {r.budget_scope for r in llm.requests} == {"level:N3"}
    assert llm.calls(names.ROLE_JUDGE)[0].max_output_tokens == 6000
    # Round-1 judge reads the passages by descending score.
    judge_passages = llm.calls(names.ROLE_JUDGE)[0].input["passages"]
    assert judge_passages[0]["id"] == "P1"


async def test_english_post_gets_french_and_english_versions(
    db: Database, settings: Settings
) -> None:
    install()
    inv_id = await seed(db, lang="en")
    await _request(db, inv_id)
    await drain(db, settings)
    translations = await rows(db, "SELECT lang FROM context_note_translations ORDER BY lang")
    assert [t.lang for t in translations] == ["en", "fr"]
    assert (await rows(db, "SELECT lang FROM context_notes"))[0].lang == "en"


async def test_replay_of_every_job_is_a_no_op(db: Database, settings: Settings) -> None:
    llm, atlas = install()
    inv_id = await seed(db)
    await _request(db, inv_id)
    jobs = await drain(db, settings)
    before = await investigation(db, inv_id)
    calls = len(llm.requests)
    counts = {
        t: await scalar(db, f"SELECT count(*) FROM {t}")  # noqa: S608
        for t in ("verdicts", "citations", "context_notes", "evidence_passages", "events", "jobs")
    }
    for job in jobs:
        await run_job(db, settings, job)
    after = await investigation(db, inv_id)
    assert len(llm.requests) == calls and len(atlas.requests) == 1
    assert after.cost_usd_micros == before.cost_usd_micros
    assert after.stage == "completed" and after.flags == before.flags
    for t, n in counts.items():
        assert await scalar(db, f"SELECT count(*) FROM {t}") == n, t  # noqa: S608


async def test_replay_between_stages_does_not_pay_twice(db: Database, settings: Settings) -> None:
    llm, _ = install()
    inv_id = await seed(db)
    await _request(db, inv_id)
    frame_job = (await drain(db, settings, count=1))[0]
    await run_job(db, settings, frame_job)  # already advanced to collecting
    inv = await investigation(db, inv_id)
    assert inv.stage == "collecting" and inv.cost_usd_micros == 1000
    assert llm.counts[names.ROLE_FRAME] == 1
    assert await scalar(db, "SELECT count(*) FROM jobs WHERE kind = 'investigate.collect'") == 1


# ----------------------------------------------------------------- framing


async def test_unverifiable_at_framing_n3_has_no_verdict(db: Database, settings: Settings) -> None:
    llm, atlas = install(
        FakeLLM({names.ROLE_FRAME: frame_output(verifiable=False, reason="Opinion.")})
    )
    inv_id = await seed(db)
    await _request(db, inv_id)
    assert kinds(await drain(db, settings)) == ["frame"]
    inv = await investigation(db, inv_id)
    assert inv.stage == "completed" and inv.flags["unverifiable_at_framing"] is True
    assert inv.framing is not None and inv.cost_usd_micros == 1000
    assert await scalar(db, "SELECT count(*) FROM verdicts") == 0
    events = await completed_events(db)
    assert [e["outcome"] for e in events] == ["unverifiable_at_framing"]
    assert events[0]["verdict_id"] is None and events[0]["veracity"] is None
    assert not atlas.requests and llm.counts[names.ROLE_QUERY_WRITER] == 0


async def test_unverifiable_at_framing_n4_drafts_without_note(
    db: Database, settings: Settings
) -> None:
    llm, _ = install(
        FakeLLM({names.ROLE_FRAME_REINFORCED: frame_output(verifiable=False, reason="Opinion.")})
    )
    inv_id = await seed(db, level="N4")
    await _request(db, inv_id)
    assert kinds(await drain(db, settings)) == ["frame", "draft"]
    verdict = (await rows(db, "SELECT * FROM verdicts"))[0]
    assert verdict.veracity == "unverifiable" and verdict.context_need == "none"
    assert verdict.review_points == ["Opinion."]
    assert verdict.flags["unverifiable_at_framing"] is True
    assert await scalar(db, "SELECT count(*) FROM context_notes") == 0
    assert [e["outcome"] for e in await completed_events(db)] == ["drafted"]
    assert llm.counts[names.ROLE_WRITER] == 0 and llm.counts[names.ROLE_FRAME_REINFORCED] == 1


async def test_prediction_claim_is_never_investigated(db: Database, settings: Settings) -> None:
    install()
    inv_id = await seed(db)
    async with db.transaction() as session:
        await session.execute(text("UPDATE claims SET claim_type = 'prediction'"))
    await _request(db, inv_id)
    assert kinds(await drain(db, settings)) == ["frame"]
    assert await scalar(db, "SELECT count(*) FROM verdicts") == 0


# -------------------------------------------------------------- routing


async def test_substantive_disagreement_objection_then_escalation(
    db: Database, settings: Settings
) -> None:
    llm, atlas = install(
        FakeLLM({names.ROLE_SECOND_OPINION: second_opinion(Veracity.FALSE)}),
        FakeAtlas([default_docs(), extra_docs()]),
    )
    inv_id = await seed(db)
    await _request(db, inv_id)
    jobs = await drain(db, settings)
    assert kinds(jobs) == [
        "frame",
        "collect",
        "judge",
        "second_opinion",
        "objection",
        "escalate",
        "collect",
        "judge",
        "draft",
    ]
    inv = await investigation(db, inv_id)
    assert inv.depth == "reinforced" and inv.round == 2 and inv.stage == "completed"
    assert inv.agreement == "substantive" and inv.second_pass is not None
    assert inv.flags["escalated"] is True and inv.flags["second_pass"] == "concordant"
    assert inv.flags["objection"]["label"] == "P1"
    assert inv.queries is not None and [r["round"] for r in inv.queries["rounds"]] == [1, 2]
    assert len(atlas.requests) == 2
    assert atlas.requests[1].max_web_searches == settings.fc_searches_reinforced_extra
    passages = await rows(db, "SELECT label, round FROM evidence_passages ORDER BY id")
    assert [p.label for p in passages] == ["P1", "P2", "P3", "P4", "P5"]
    assert [p.round for p in passages] == [1, 1, 1, 1, 2]
    # Second pass: reinforced judge, every passage, reversed.
    second_pass_request = llm.calls(names.ROLE_JUDGE_REINFORCED)[0]
    assert [p["id"] for p in second_pass_request.input["passages"]] == [
        "P5",
        "P4",
        "P3",
        "P2",
        "P1",
    ]
    assert llm.counts[names.ROLE_FRAME] == 1 and llm.counts[names.ROLE_FRAME_REINFORCED] == 0
    verdict = (await rows(db, "SELECT * FROM verdicts"))[0]
    assert verdict.flags["depth"] == "reinforced" and verdict.flags["round"] == 2
    assert verdict.flags["objection"]["explanation"] == "Les preuves sont partielles."
    assert verdict.review_points[-1] == "Les preuves sont partielles."
    assert verdict.flags["second_opinion_veracity"] == "false"


async def test_discordant_second_pass_adds_review_point(db: Database, settings: Settings) -> None:
    install(
        FakeLLM(
            {
                names.ROLE_SECOND_OPINION: second_opinion(Veracity.FALSE),
                names.ROLE_JUDGE_REINFORCED: lambda r: judge_output(r, Veracity.MISLEADING),
            }
        ),
        FakeAtlas([default_docs(), extra_docs()]),
    )
    inv_id = await seed(db)
    await _request(db, inv_id)
    await drain(db, settings)
    verdict = (await rows(db, "SELECT * FROM verdicts"))[0]
    assert verdict.veracity == "misleading"  # the final verdict is the second pass
    assert verdict.flags["second_pass"] == "discordant"
    assert verdict.review_points[-1] == "Les deux passes du juge divergent : true puis misleading."


async def test_n4_always_runs_a_second_pass_without_new_collection(
    db: Database, settings: Settings
) -> None:
    llm, atlas = install()
    inv_id = await seed(db, level="N4")
    await _request(db, inv_id)
    jobs = await drain(db, settings)
    assert kinds(jobs) == [
        "frame",
        "collect",
        "judge",
        "second_opinion",
        "escalate",
        "judge",
        "draft",
    ]
    inv = await investigation(db, inv_id)
    assert inv.round == 2 and inv.depth == "reinforced" and "escalated" not in inv.flags
    assert len(atlas.requests) == 1
    assert atlas.requests[0].max_web_searches == 8
    assert llm.counts[names.ROLE_JUDGE_REINFORCED] == 2 and llm.counts[names.ROLE_JUDGE] == 0
    assert inv.second_pass is not None and inv.flags["second_pass"] == "concordant"
    assert {r.budget_scope for r in llm.requests} == {"level:N4"}


async def test_cost_cap_blocks_escalation(db: Database, settings: Settings) -> None:
    _llm, atlas = install(FakeLLM({names.ROLE_SECOND_OPINION: second_opinion(Veracity.FALSE)}))
    capped = make_settings(fc_cap_standard_usd=0.003)
    inv_id = await seed(db)
    await _request(db, inv_id)
    jobs = await drain(db, capped)
    assert "collect" not in kinds(jobs)[2:] and kinds(jobs).count("collect") == 1
    inv = await investigation(db, inv_id)
    assert inv.depth == "standard" and inv.round == 1
    assert inv.flags["cap_reached"] is True and "escalated" not in inv.flags
    assert len(atlas.requests) == 1
    verdict = (await rows(db, "SELECT flags FROM verdicts"))[0]
    assert verdict.flags["cap_reached"] is True


# --------------------------------------------------------------- draft checks


async def test_invalid_quote_is_dropped_and_captured(db: Database, settings: Settings) -> None:
    install(FakeLLM({names.ROLE_JUDGE: lambda r: judge_output(r, fabricate=True)}))
    inv_id = await seed(db)
    await _request(db, inv_id)
    await drain(db, settings)
    verdict = (await rows(db, "SELECT * FROM verdicts"))[0]
    assert "Une affirmation inventée." not in [r["assertion"] for r in verdict.rationale]
    assert len(verdict.rationale) == 3
    citations = await rows(db, "SELECT valid, assertion FROM citations ORDER BY position")
    assert [c.valid for c in citations].count(False) == 1
    assert next(c for c in citations if not c.valid).assertion == "Une affirmation inventée."
    assert verdict.flags["checks"]["invalid_quotes"] == ["P1"]
    await get_reporter().flush()
    codes = [r.code for r in await rows(db, "SELECT code FROM error_reports")]
    assert "TWR-FCK-014" in codes
    context = await scalar(
        db, "SELECT last_context::text FROM error_reports WHERE code = 'TWR-FCK-014'"
    )
    assert "never appear" not in context  # the quote text never reaches the error context


async def test_true_without_independent_primary_is_downgraded(
    db: Database, settings: Settings
) -> None:
    install(
        FakeLLM(
            {
                names.ROLE_JUDGE: lambda r: judge_output(
                    r, Veracity.TRUE, only_domains=["lemonde.fr"]
                )
            }
        )
    )
    inv_id = await seed(db)
    await _request(db, inv_id)
    await drain(db, settings)
    verdict = (await rows(db, "SELECT * FROM verdicts"))[0]
    assert verdict.veracity == "unverifiable" and verdict.context_need == "useful"
    assert verdict.flags["downgraded"] is True and verdict.flags["independent_origins"] == 1
    assert verdict.flags["has_primary"] is False
    assert len(verdict.rationale) == 1


async def test_judge_refusal_gives_a_flagged_unverifiable_draft(
    db: Database, settings: Settings
) -> None:
    llm, _ = install(FakeLLM({names.ROLE_JUDGE: AllRefused("refused")}))
    inv_id = await seed(db)
    await _request(db, inv_id)
    assert kinds(await drain(db, settings)) == ["frame", "collect", "judge", "draft"]
    verdict = (await rows(db, "SELECT * FROM verdicts"))[0]
    assert verdict.veracity == "unverifiable"
    assert verdict.flags["refused"] == ["judge"]
    assert verdict.review_points == ["Le modèle juge a refusé de traiter cette affirmation."]
    assert await scalar(db, "SELECT count(*) FROM context_notes") == 0
    assert llm.counts[names.ROLE_WRITER] == 0 and llm.counts[names.ROLE_SECOND_OPINION] == 0
    await get_reporter().flush()
    assert "TWR-FCK-005" in [r.code for r in await rows(db, "SELECT code FROM error_reports")]


async def test_second_opinion_refusal_continues_without_agreement(
    db: Database, settings: Settings
) -> None:
    install(FakeLLM({names.ROLE_SECOND_OPINION: AllRefused("refused")}))
    inv_id = await seed(db)
    await _request(db, inv_id)
    jobs = await drain(db, settings)
    assert "objection" not in kinds(jobs)
    inv = await investigation(db, inv_id)
    assert inv.agreement is None and inv.flags["refused"] == ["second_opinion"]
    verdict = (await rows(db, "SELECT veracity FROM verdicts"))[0]
    assert verdict.veracity == "true"


async def test_writer_refusal_completes_without_note(db: Database, settings: Settings) -> None:
    install(FakeLLM({names.ROLE_WRITER: AllRefused("refused")}))
    inv_id = await seed(db)
    await _request(db, inv_id)
    await drain(db, settings)
    verdict = (await rows(db, "SELECT * FROM verdicts"))[0]
    assert verdict.flags["refused"] == ["writer"] and verdict.veracity == "true"
    assert await scalar(db, "SELECT count(*) FROM context_notes") == 0


async def test_no_passage_skips_the_judge(db: Database, settings: Settings) -> None:
    llm, _ = install(atlas=FakeAtlas([[]]))
    inv_id = await seed(db)
    await _request(db, inv_id)
    assert kinds(await drain(db, settings)) == ["frame", "collect", "draft"]
    assert llm.counts[names.ROLE_JUDGE] == 0 and llm.counts[names.ROLE_WRITER] == 0
    verdict = (await rows(db, "SELECT * FROM verdicts"))[0]
    assert verdict.veracity == "unverifiable" and verdict.context_need == "none"
    assert verdict.flags["no_evidence"] is True
    assert await scalar(db, "SELECT count(*) FROM evidence_passages") == 0
    inv = await investigation(db, inv_id)
    assert (
        inv.judgement is not None
        and inv.judgement["needs_more_research"] == "Aucune source récupérée."
    )


# ----------------------------------------------------- budget and failure handling


def _exhausted(hours: float = 1.0) -> BudgetExhausted:
    return BudgetExhausted("level:N3", datetime.now(UTC) + timedelta(hours=hours))


async def test_budget_exhausted_retries_later_with_clamped_delay(
    db: Database, settings: Settings
) -> None:
    install(FakeLLM({names.ROLE_JUDGE: _exhausted(), names.ROLE_FRAME: [_exhausted(0.0), None]}))
    inv_id = await seed(db)
    await _request(db, inv_id)
    with pytest.raises(RetryLater) as soon:
        await drain(db, settings)
    assert soon.value.delay == 60.0  # already due: clamped to the minimum
    inv = await investigation(db, inv_id)
    assert inv.stage == "framing" and inv.cost_usd_micros == 0
    # The retried frame job runs again and advances; the judge then waits for its budget.
    await requeue(db, names.JOB_FRAME, inv_id)
    with pytest.raises(RetryLater) as later:
        await drain(db, settings)
    assert later.value.delay > 3000
    inv = await investigation(db, inv_id)
    assert inv.stage == "judging"


async def test_budget_exhausted_on_last_attempt_skips_optional_stages(
    db: Database, settings: Settings
) -> None:
    install(FakeLLM({names.ROLE_SECOND_OPINION: _exhausted()}))
    inv_id = await seed(db)
    await _request(db, inv_id)
    jobs = await drain(db, settings, attempts=5, max_attempts=5)
    assert kinds(jobs) == ["frame", "collect", "judge", "second_opinion", "draft"]
    inv = await investigation(db, inv_id)
    assert inv.stage == "completed" and inv.flags["budget_incomplete"] is True
    verdict = (await rows(db, "SELECT * FROM verdicts"))[0]
    assert verdict.flags["budget_incomplete"] is True and verdict.veracity == "true"


async def test_writer_budget_exhausted_on_last_attempt_completes_without_note(
    db: Database, settings: Settings
) -> None:
    install(FakeLLM({names.ROLE_WRITER: _exhausted()}))
    inv_id = await seed(db)
    await _request(db, inv_id)
    # Not the last attempt: the draft job waits.
    with pytest.raises(RetryLater):
        await drain(db, settings)
    assert (await investigation(db, inv_id)).stage == "drafting"
    # Last attempt: the draft is completed without note, flagged.
    await requeue(db, names.JOB_DRAFT, inv_id)
    await drain(db, settings, attempts=5, max_attempts=5)
    inv = await investigation(db, inv_id)
    assert inv.stage == "completed"
    verdict = (await rows(db, "SELECT * FROM verdicts"))[0]
    assert verdict.flags["budget_incomplete"] is True
    assert await scalar(db, "SELECT count(*) FROM context_notes") == 0


async def test_failure_on_last_attempt_marks_the_investigation_failed(
    db: Database, settings: Settings
) -> None:
    install(FakeLLM({names.ROLE_JUDGE: NoValidOutput("schema mismatch")}))
    inv_id = await seed(db)
    await _request(db, inv_id)
    # Earlier attempts re-raise for the normal backoff, without touching the state.
    with pytest.raises(NoValidOutput):
        await drain(db, settings, attempts=2)
    assert (await investigation(db, inv_id)).stage == "judging"
    await requeue(db, names.JOB_JUDGE, inv_id)
    with pytest.raises(PermanentError):
        await drain(db, settings, attempts=5, max_attempts=5)
    inv = await investigation(db, inv_id)
    assert inv.stage == "failed" and inv.error is not None and "judging" in inv.error
    assert len(inv.error) <= 500
    events = await completed_events(db)
    assert [e["outcome"] for e in events] == ["failed"] and events[0]["verdict_id"] is None
    await get_reporter().flush()
    assert "TWR-FCK-006" in [r.code for r in await rows(db, "SELECT code FROM error_reports")]
    assert await scalar(db, "SELECT count(*) FROM verdicts") == 0


async def test_provider_not_configured_is_permanent(db: Database, settings: Settings) -> None:
    install(FakeLLM({names.ROLE_FRAME: ProviderNotConfigured("no key")}))
    inv_id = await seed(db)
    await _request(db, inv_id)
    with pytest.raises(PermanentError):
        await drain(db, settings)
    assert (await investigation(db, inv_id)).stage == "failed"
    assert [e["outcome"] for e in await completed_events(db)] == ["failed"]
