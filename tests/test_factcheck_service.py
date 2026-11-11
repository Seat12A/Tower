"""Service ``factcheck``: subscriptions, extraction (E2), manual requests, N4 promotion.

Real PostgreSQL; the model is scripted (``ScriptedLLM``) or the fake provider behind a real
router; the platform is a fake client injected with ``runtime.set_override``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import structlog
from sqlalchemy import text

from tower.core.adapters.fake_llm import FakeEmbedder
from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain.factcheck.values import ClaimType, EntityRole, Polarity
from tower.core.domain.ingestion import AuthorInput, ItemInput
from tower.core.domain.llm import TokenUsage
from tower.core.domain.security import Actor
from tower.core.events.outbox import EventRecord
from tower.core.factcheck import names, runtime
from tower.core.factcheck.schemas import ClaimEntity, ExtractedClaim, ExtractOutput, Signature
from tower.core.jobs.queue import JobRecord, PermanentError, RetryLater
from tower.core.llm.admin import run_seed
from tower.core.llm.embeddings import EmbedResult
from tower.core.llm.errors import AllRefused, BudgetExhausted
from tower.core.ports.llm import LLMRequest, LLMResult
from tower.core.ports.platforms import (
    ContentItem as PlatformItem,
)
from tower.core.ports.platforms import (
    Metrics,
    PlatformBadResponse,
    PlatformRateLimited,
    PlatformUnavailable,
    ThreadContext,
)
from tower.core.services.base import JobContext
from tower.core.services.registry import load_services
from tower.core.usecases import ingestion as ing
from tower.core.usecases import watchlist as watch_uc
from tower.services.factcheck.jobs import (
    REPLAY_NOTE,
    extract,
    on_content_ingested,
    on_investigation_completed,
    on_verdict_published,
    request,
)

from .conftest import make_settings

SYSTEM = Actor.system("test")
NOW = datetime.now(UTC)
AUTHOR = "5000"


# ------------------------------------------------------------------------- helpers


class ScriptedLLM:
    """``LLMClient`` replaying a script of outputs (last item repeats); exceptions raise."""

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return LLMResult(
            output=item,
            provider="fake",
            model="fake",
            usage=TokenUsage(),
            cost_usd_micros=0,
            free_tier=False,
            cache_hit=False,
            request_id="r",
            prompt_version="pv1",
            route_version=1,
        )


class FakePlatform:
    platform = "x"
    cost_per_read_usd_micros = 5_000

    def __init__(self, item: PlatformItem | None = None, error: Exception | None = None) -> None:
        self.item = item
        self.error = error
        self.calls: list[str] = []

    async def get_item(self, external_id: str) -> PlatformItem | None:
        self.calls.append(external_id)
        if self.error is not None:
            raise self.error
        return self.item

    async def get_thread_context(self, item: PlatformItem) -> ThreadContext:
        return ThreadContext()

    async def get_metrics(self, external_id: str) -> Metrics | None:
        return None

    async def get_account(self, account_ref: str) -> None:
        return None

    async def poll_account(self, account_ref: str, *, since: str | None) -> Sequence[PlatformItem]:
        return []


def signature(**kw: Any) -> Signature:
    base: dict[str, Any] = {
        "subject": None,
        "subject_qid": None,
        "predicate": None,
        "value_low": None,
        "value_high": None,
        "unit": None,
        "unit_scale": None,
        "currency": None,
        "value_date": None,
        "period_start": None,
        "period_end": None,
        "place": None,
        "polarity": Polarity.AFFIRMATIVE,
        "invoked_source": None,
        "definition_variant": None,
        "comparison_base": None,
    }
    base.update(kw)
    return Signature(**base)


def claim(
    text_: str,
    *,
    claim_type: ClaimType = ClaimType.STATISTIC,
    entities: Sequence[str] = (),
    sig: Signature | None = None,
) -> ExtractedClaim:
    return ExtractedClaim(
        text_original=text_,
        text_fr=text_,
        claim_type=claim_type,
        signature=sig or signature(),
        jurisdictions=["FR"],
        primary_jurisdiction="FR",
        entities=[ClaimEntity(name=e, role=EntityRole.SUBJECT, qid=None) for e in entities],
        theme="economy",
        verifiability=0.8,
        context_hints=[],
        source_modality="text",
    )


def output(
    content_id: int,
    claims: Sequence[ExtractedClaim] = (),
    absence: str | None = None,
    thread: str | None = None,
) -> ExtractOutput:
    return ExtractOutput.model_validate(
        {
            "post_id": str(content_id),
            "claims": [c.model_dump(mode="json") for c in claims],
            "absence_reason": absence,
            "thread_context": thread,
        }
    )


def ctx(
    db: Database, settings: Settings, kind: str, payload: dict[str, Any], *, attempts: int = 1
) -> JobContext:
    job = JobRecord(
        id=1,
        queue=names.FC_FAST,
        kind=kind,
        payload=payload,
        priority=0,
        attempts=attempts,
        max_attempts=3,
        dedupe_key=None,
        created_at=NOW,
    )
    return JobContext(job=job, db=db, settings=settings, log=structlog.get_logger("test"))


async def make_content(
    db: Database,
    ext: str,
    text_: str,
    *,
    author: str | None = AUTHOR,
    lang: str | None = "fr",
    created: datetime | None = None,
    metrics: dict[str, int] | None = None,
    parent: str | None = None,
    quoted: str | None = None,
) -> int:
    async with db.transaction() as session:
        await ing.ingest_items(
            session,
            SYSTEM,
            [
                ItemInput(
                    platform="x",
                    external_id=ext,
                    text=text_,
                    author=AuthorInput(external_id=author, handle="someone") if author else None,
                    lang_hint=lang,
                    created_at=created or NOW - timedelta(hours=1),
                    metrics=metrics or {},
                    parent_external_id=parent,
                    quoted_external_id=quoted,
                )
            ],
            source="radar",
            filter_version=None,
            installation_token=None,
            max_items=10,
            daily_quota=10,
        )
    return int(await scalar(db, "SELECT id FROM content_items WHERE external_id = :e", e=ext))


async def scalar(db: Database, sql: str, **params: Any) -> Any:
    async with db.session() as s:
        return (await s.execute(text(sql), params)).scalar_one()


async def rows(db: Database, sql: str, **params: Any) -> list[Any]:
    async with db.session() as s:
        return list((await s.execute(text(sql), params)).all())


async def watch(db: Database, ext: str = AUTHOR, *, active: bool = True) -> None:
    async with db.transaction() as s:
        w, _ = await watch_uc.add_watched_account(
            s, SYSTEM, platform="x", external_id=ext, category="media_outlet"
        )
        if not active:
            await s.execute(
                text("UPDATE watched_accounts SET active = false WHERE id = :i"), {"i": w.id}
            )


def event(topic: str, payload: dict[str, Any], id_: int = 1) -> EventRecord:
    return EventRecord(
        id=id_,
        xid=1,
        topic=topic,
        aggregate_type=None,
        aggregate_id=None,
        payload=payload,
        actor=None,
        created_at=NOW,
    )


async def jobs_of(db: Database, kind: str) -> list[dict[str, Any]]:
    result = await rows(
        db, "SELECT payload, dedupe_key FROM jobs WHERE kind = :k ORDER BY id", k=kind
    )
    return [{**r[0], "_dedupe": r[1]} for r in result]


class StubEmbedder:
    """Deterministic embeddings without budget or route (E3 is not under test here)."""

    model_name = "stub-embedding"

    def __init__(self) -> None:
        self._fake = FakeEmbedder()

    async def embed(self, texts: Sequence[str], *, level: str, ref: str) -> EmbedResult:
        vectors = await self._fake.embed(texts, dimensions=512)
        return EmbedResult(vectors, self.model_name, 0, 0)


@pytest.fixture(autouse=True)
def _reset_runtime() -> Iterator[None]:
    runtime.clear_overrides()
    runtime.set_override("embedder", StubEmbedder())
    yield
    runtime.clear_overrides()


# ------------------------------------------------------------ seeding (assembly tests)


async def seed_investigation(
    session: Any,
    content_id: int,
    *,
    stage: str = "completed",
    level: str = "N3",
    with_verdict: bool = True,
    veracity: str = "mostly_true",
    flags: dict[str, Any] | None = None,
    verdict_flags: dict[str, Any] | None = None,
    claim_type: str = "statistic",
    verdict_status: str = "draft",
    cited: Sequence[tuple[str, str, str | None, int]] = (),
    agreement: str | None = None,
) -> Any:
    """Claim + group + investigation (+ draft verdict with cited passages).

    ``cited``: ``(label, "domain|tier", origin, simhash)`` per valid citation."""
    from tower.core.models.factcheck import (
        Citation,
        Claim,
        ClaimGroup,
        ClaimGroupMember,
        ContentClaim,
        EvidencePassage,
        Investigation,
        Source,
        Verdict,
    )

    claim_row = Claim(
        public_id=uuid.uuid4(),
        lang_original="fr",
        text_original="Le chômage est de 7 %.",
        text_fr="Le chômage est de 7 %.",
        claim_type=claim_type,
        verifiability=0.8,
        prompt_version="pv1",
    )
    session.add(claim_row)
    await session.flush()
    session.add(ContentClaim(content_id=content_id, claim_id=claim_row.id, position=0))
    group = ClaimGroup(
        public_id=uuid.uuid4(), representative_claim_id=claim_row.id, level=level, state="queued"
    )
    session.add(group)
    await session.flush()
    session.add(ClaimGroupMember(group_id=group.id, claim_id=claim_row.id))
    inv = Investigation(
        public_id=uuid.uuid4(),
        claim_group_id=group.id,
        content_id=content_id,
        level=level,
        depth="reinforced" if level == "N4" else "standard",
        stage=stage,
        requested_by="system:factcheck",
        framing={"known_pitfalls": []},
        flags=flags or {},
        agreement=agreement,
    )
    session.add(inv)
    await session.flush()
    if not with_verdict:
        return inv
    verdict = Verdict(
        public_id=uuid.uuid4(),
        claim_group_id=group.id,
        investigation_id=inv.id,
        veracity=veracity,
        context_need="none",
        status=verdict_status,
        flags=verdict_flags or {},
    )
    session.add(verdict)
    await session.flush()
    for n, (label, spec, origin, simhash) in enumerate(cited):
        domain, _, tier = spec.partition("|")
        source = Source(
            canonical_url=f"https://{domain}/{uuid.uuid4()}",
            original_url=f"https://{domain}/x",
            adapter="brave",
            domain=domain,
            tier=tier or "tertiary",
            publisher=domain,
        )
        session.add(source)
        await session.flush()
        passage = EvidencePassage(
            investigation_id=inv.id,
            source_id=source.id,
            label=label,
            position=n,
            text="passage text",
            lang="fr",
            origin=origin,
            simhash=simhash,
        )
        session.add(passage)
        await session.flush()
        session.add(
            Citation(
                verdict_id=verdict.id,
                passage_id=passage.id,
                quote="passage",
                assertion="assertion",
                valid=True,
                position=n,
            )
        )
    await session.flush()
    return inv


# --------------------------------------------------------------- service declaration


def test_service_is_declared_and_valid(settings: Settings) -> None:
    service = load_services(["factcheck"])
    assert {s.kind for s in service.services[0].jobs} == {
        names.JOB_EXTRACT,
        names.JOB_REQUEST,
        names.JOB_ASSEMBLE,
        names.JOB_TRANSLATE,
        names.JOB_EMBED_BATCH,
        names.JOB_REVALIDATE,
        names.JOB_INSEE_REFRESH,
    }
    assert {p.name for p in service.services[0].periodic} == {
        "factcheck:drain_quota",
        "factcheck:purge_source_cache",
        "factcheck:embedding_partitions",
        "factcheck:expire",
        "factcheck:reference_feed",
    }
    assert sorted(service.queues) == sorted([names.FC_FAST, names.FC_BULK])
    service.validate(lease_seconds=settings.job_lease_seconds * 10)


# ---------------------------------------------------------------------- subscriptions


async def test_content_ingested_filter_and_dedupe(db: Database) -> None:
    await watch(db, "5000")
    await watch(db, "5001", active=False)
    watched = await make_content(db, "1001", "Un texte de test un", author="5000")
    unwatched = await make_content(db, "1002", "Un texte de test deux", author="6000")
    inactive = await make_content(db, "1003", "Un texte de test trois", author="5001")
    copy = await make_content(db, "1004", "Un texte de test quatre", author="5000")
    wrong_lang = await make_content(db, "1005", "Ein Text zum Testen fünf", author="5000")
    no_author = await make_content(db, "1006", "Un texte de test six", author=None)
    async with db.transaction() as s:
        await s.execute(
            text("UPDATE content_items SET copy_of_id = :o WHERE id = :c"),
            {"o": watched, "c": copy},
        )
    ids = [watched, unwatched, inactive, copy, wrong_lang, no_author]
    events = [
        event("content.ingested", {"id": str(i), "lang": "de" if i == wrong_lang else "fr"}, n)
        for n, i in enumerate(ids, start=1)
    ]
    for _ in range(2):  # the second delivery is deduplicated
        async with db.transaction() as s:
            await on_content_ingested(s, events)
    queued = await jobs_of(db, names.JOB_EXTRACT)
    assert [q["content_id"] for q in queued] == [watched]
    assert queued[0]["level"] == "N3"
    assert queued[0]["requested_by"] == names.FACTCHECK_ACTOR
    assert queued[0]["_dedupe"] == names.extract_key(watched, "N3")


async def test_investigation_completed_enqueues_assembly(db: Database) -> None:
    await watch(db)
    cid = await make_content(db, "1010", "Le chômage est à 7 % en France.")
    async with db.transaction() as s:
        inv = await seed_investigation(s, cid, stage="completed", with_verdict=False)
    async with db.transaction() as s:
        await on_investigation_completed(
            s,
            [
                event("investigation.completed", {"investigation_id": str(inv.public_id)}, 1),
                event(
                    "investigation.completed",
                    {"investigation_id": str(uuid.uuid4()), "outcome": "failed"},
                    2,
                ),
                event("investigation.completed", {"investigation_id": "not-a-uuid"}, 3),
            ],
        )
    queued = await jobs_of(db, names.JOB_ASSEMBLE)
    assert queued == [{"investigation_id": inv.id, "_dedupe": names.assemble_key(inv.id)}]


# ------------------------------------------------------------------------ extraction

POST = "Le chômage est passé à 7 % en France selon l'INSEE."


async def run_extract(
    db: Database, settings: Settings, cid: int, level: str = "N3", *, by: str = "system:factcheck"
) -> None:
    await extract(
        ctx(
            db, settings, names.JOB_EXTRACT, {"content_id": cid, "level": level, "requested_by": by}
        )
    )


async def test_extract_records_claim_group_investigation_and_events(
    db: Database, settings: Settings
) -> None:
    cid = await make_content(db, "2001", POST, metrics={"impressions": 1234})
    good = claim(
        "Le taux de chômage est de 7 % en France.",
        entities=["INSEE"],
        sig=signature(
            value_low=6.5,
            value_high=7.5,
            predicate="unemployment_rate",
            value_date="2024-13-45",
            period_start="2024-01-01",
        ),
    )
    llm = ScriptedLLM(output(cid, [good], thread="Replies to something."))
    runtime.set_override("llm", llm)

    await run_extract(db, settings, cid)

    assert len(llm.requests) == 1
    req = llm.requests[0]
    assert req.role == names.ROLE_EXTRACT and req.level.value == "N3"
    assert req.budget_scope == "level:N3" and req.input["post"]["id"] == str(cid)
    assert "replay" not in req.input
    claims = await rows(
        db,
        "SELECT lang_original, claim_type, value_low, value_high, value_date, period_start,"
        " prompt_version, thread_context, entities FROM claims",
    )
    assert len(claims) == 1
    c = claims[0]
    assert c[0] == "fr" and c[1] == "statistic" and float(c[2]) == 6.5 and float(c[3]) == 7.5
    assert c[4] is None and str(c[5]) == "2024-01-01"  # invalid date → NULL
    assert c[6] == "pv1" and c[7] == "Replies to something."
    assert c[8][0]["name"] == "INSEE"
    assert await scalar(db, "SELECT count(*) FROM content_claims WHERE content_id = :c", c=cid) == 1
    group = (
        await rows(db, "SELECT level, state, reach, representative_claim_id FROM claim_groups")
    )[0]
    assert (group[0], group[1], group[2]) == ("N3", "queued", 1234)
    assert (
        await scalar(db, "SELECT count(*) FROM claim_group_members WHERE removed_at IS NULL") == 1
    )
    inv = (
        await rows(
            db,
            "SELECT level, depth, stage, content_id, requested_by, flags FROM investigations",
        )
    )[0]
    assert tuple(inv[:5]) == ("N3", "standard", "framing", cid, "system:factcheck")
    assert inv[5] == {}
    events = await rows(db, "SELECT topic, payload, xid FROM events ORDER BY id")
    topics = [e[0] for e in events if e[0] in ("claim.extracted", "investigation.requested")]
    assert topics == ["investigation.requested", "claim.extracted"]
    extracted = next(e for e in events if e[0] == "claim.extracted")
    requested = next(e for e in events if e[0] == "investigation.requested")
    assert extracted[1]["content_id"] == str(cid) and extracted[1]["level"] == "N3"
    assert extracted[1]["platform"] == "x" and extracted[1]["external_id"] == "2001"
    assert len(extracted[1]["claim_ids"]) == 1 and len(extracted[1]["group_ids"]) == 1
    assert requested[1]["group_id"] == extracted[1]["group_ids"][0]
    assert requested[1]["depth"] == "standard" and requested[1]["content_id"] == str(cid)
    assert extracted[2] == requested[2]  # same transaction


async def test_opinion_claims_get_a_group_but_no_investigation(
    db: Database, settings: Settings
) -> None:
    cid = await make_content(db, "2002", "Ce gouvernement est le pire que la France ait connu.")
    runtime.set_override(
        "llm",
        ScriptedLLM(
            output(cid, [claim("Ce gouvernement est le pire.", claim_type=ClaimType.OPINION)])
        ),
    )
    await run_extract(db, settings, cid)
    assert await scalar(db, "SELECT count(*) FROM claim_groups") == 1
    assert await scalar(db, "SELECT count(*) FROM investigations") == 0
    assert await scalar(db, "SELECT count(*) FROM events WHERE topic = 'claim.extracted'") == 1
    assert (
        await scalar(db, "SELECT count(*) FROM events WHERE topic = 'investigation.requested'") == 0
    )


async def test_absence_reason_writes_nothing(db: Database, settings: Settings) -> None:
    cid = await make_content(db, "2003", "Quelle honte, vraiment, quelle triste époque pour tous.")
    llm = ScriptedLLM(output(cid, absence="opinion"))
    runtime.set_override("llm", llm)
    await run_extract(db, settings, cid)
    assert len(llm.requests) == 1  # nothing to replay: no figure in the post
    assert await scalar(db, "SELECT count(*) FROM claims") == 0
    assert await scalar(db, "SELECT count(*) FROM events WHERE topic LIKE 'claim%'") == 0


async def test_contaminated_claim_is_dropped_and_extraction_replayed(
    db: Database, settings: Settings
) -> None:
    cid = await make_content(db, "2004", POST)
    bad = claim("Le taux de chômage est de 9 % en France.")
    good = claim("Le taux de chômage est de 7 % en France.", entities=["INSEE"])
    llm = ScriptedLLM(output(cid, [bad]), output(cid, [good]))
    runtime.set_override("llm", llm)

    await run_extract(db, settings, cid)

    assert len(llm.requests) == 2
    assert "replay" not in llm.requests[0].input
    assert llm.requests[1].input["replay"] == REPLAY_NOTE
    assert {k: v for k, v in llm.requests[1].input.items() if k != "replay"} == dict(
        llm.requests[0].input
    )
    texts = [r[0] for r in await rows(db, "SELECT text_original FROM claims")]
    assert texts == ["Le taux de chômage est de 7 % en France."]


async def test_contaminated_entity_and_replay_keeps_only_faithful_claims(
    db: Database, settings: Settings
) -> None:
    cid = await make_content(db, "2005", POST)
    faithful = claim("Le chômage est de 7 %.", entities=["INSEE"])
    invented = claim("Le chômage est de 7 % selon Eurostat.", entities=["Eurostat"])
    llm = ScriptedLLM(output(cid, [faithful, invented]), output(cid, [invented]))
    runtime.set_override("llm", llm)
    await run_extract(db, settings, cid)
    assert len(llm.requests) == 2
    assert await scalar(db, "SELECT count(*) FROM claims") == 0  # replay output is unfaithful too
    assert await scalar(db, "SELECT count(*) FROM investigations") == 0


async def test_signature_bounds_derived_from_the_post_are_not_contamination(
    db: Database, settings: Settings
) -> None:
    cid = await make_content(db, "2006", "Le déficit atteint 2 millions d'euros cette année là.")
    c = claim(
        "Le déficit atteint 2 millions d'euros.",
        sig=signature(value_low=2_000_000.0, value_high=2_000_000.0, unit_scale="million"),
    )
    llm = ScriptedLLM(output(cid, [c]))
    runtime.set_override("llm", llm)
    await run_extract(db, settings, cid)
    assert len(llm.requests) == 1
    assert await scalar(db, "SELECT count(*) FROM claims") == 1


async def test_omission_triggers_a_replay(db: Database, settings: Settings) -> None:
    cid = await make_content(
        db, "2007", "Il y a eu 12 000 morts sur les routes, un record historique."
    )
    without_figure = claim(
        "Les routes ont fait un nombre record de morts.", claim_type=ClaimType.OTHER
    )
    with_figure = claim("Il y a eu 12 000 morts sur les routes.")
    llm = ScriptedLLM(output(cid, [without_figure]), output(cid, [with_figure]))
    runtime.set_override("llm", llm)
    await run_extract(db, settings, cid)
    assert len(llm.requests) == 2
    assert [r[0] for r in await rows(db, "SELECT text_original FROM claims")] == [
        "Il y a eu 12 000 morts sur les routes."
    ]


async def test_context_of_a_reply_goes_to_the_model(db: Database, settings: Settings) -> None:
    await make_content(db, "2008", "Le budget de la culture a doublé depuis dix ans environ.")
    cid = await make_content(
        db, "2009", "C'est faux, il a baissé de moitié en réalité.", parent="2008"
    )
    llm = ScriptedLLM(output(cid, absence="no_claim"))
    runtime.set_override("llm", llm)
    await run_extract(db, settings, cid)
    context = llm.requests[0].input["post"]["context"]
    assert context["kind"] == "reply" and "budget" in context["text"].replace("ˆ", " ")


async def test_post_id_mismatch_is_retried(db: Database, settings: Settings) -> None:
    cid = await make_content(db, "2010", POST)
    runtime.set_override("llm", ScriptedLLM(output(cid + 1, [claim("Le chômage est de 7 %.")])))
    with pytest.raises(RuntimeError):
        await run_extract(db, settings, cid)
    assert await scalar(db, "SELECT count(*) FROM claims") == 0


async def test_model_errors(db: Database, settings: Settings) -> None:
    cid = await make_content(db, "2011", POST)
    retry_at = datetime.now(UTC) + timedelta(seconds=5)
    runtime.set_override("llm", ScriptedLLM(BudgetExhausted("global", retry_at)))
    with pytest.raises(RetryLater) as info:
        await run_extract(db, settings, cid)
    assert info.value.delay >= 60  # clamped
    runtime.set_override(
        "llm", ScriptedLLM(BudgetExhausted("global", datetime.now(UTC) + timedelta(days=30)))
    )
    with pytest.raises(RetryLater) as info:
        await run_extract(db, settings, cid)
    assert info.value.delay <= 6 * 3600
    runtime.set_override("llm", ScriptedLLM(AllRefused("refused")))
    await run_extract(db, settings, cid)  # captured, no claim, done
    assert await scalar(db, "SELECT count(*) FROM claims") == 0


async def test_bad_payload_and_missing_content(db: Database, settings: Settings) -> None:
    with pytest.raises(PermanentError):
        await extract(ctx(db, settings, names.JOB_EXTRACT, {"level": "N3"}))
    with pytest.raises(PermanentError):
        await extract(ctx(db, settings, names.JOB_EXTRACT, {"content_id": 1, "level": "N1"}))
    await run_extract(db, settings, 99999)  # no content: no-op


async def test_unprocessed_language_n3_skipped_n4_flagged(db: Database, settings: Settings) -> None:
    cid = await make_content(db, "2012", "Der Preis stieg um 12 Prozent im letzten Jahr deutlich.")
    async with db.transaction() as s:
        await s.execute(text("UPDATE content_items SET lang = 'de' WHERE id = :i"), {"i": cid})
    llm = ScriptedLLM(output(cid, [claim("Der Preis stieg um 12 Prozent.")]))
    runtime.set_override("llm", llm)
    await run_extract(db, settings, cid, "N3")
    assert llm.requests == []
    await run_extract(db, settings, cid, "N4", by="staff:42")
    assert len(llm.requests) == 1
    inv = (await rows(db, "SELECT level, depth, flags, requested_by FROM investigations"))[0]
    assert inv[0] == "N4" and inv[1] == "reinforced"
    assert inv[2] == {"language_not_processed": True} and inv[3] == "staff:42"


async def test_copy_inherits_claims_without_extraction(db: Database, settings: Settings) -> None:
    original = await make_content(db, "2020", POST)
    copy = await make_content(db, "2021", POST, author="7000")
    llm = ScriptedLLM(output(original, [claim("Le chômage est de 7 %.", entities=["INSEE"])]))
    runtime.set_override("llm", llm)
    await run_extract(db, settings, original)
    await run_extract(db, settings, copy)
    assert len(llm.requests) == 1  # no second model call
    assert await scalar(db, "SELECT count(*) FROM claims") == 1
    assert (
        await scalar(db, "SELECT count(*) FROM content_claims WHERE content_id = :c", c=copy) == 1
    )
    assert await scalar(db, "SELECT count(*) FROM investigations") == 1
    # Running the original again is a no-op as well.
    await run_extract(db, settings, original)
    assert len(llm.requests) == 1


async def test_n4_on_already_extracted_content_promotes_the_group(
    db: Database, settings: Settings
) -> None:
    cid = await make_content(db, "2030", POST)
    llm = ScriptedLLM(output(cid, [claim("Le chômage est de 7 %.", entities=["INSEE"])]))
    runtime.set_override("llm", llm)
    await run_extract(db, settings, cid, "N3")
    await run_extract(db, settings, cid, "N4", by="staff:7")
    await run_extract(db, settings, cid, "N4", by="staff:7")  # idempotent
    assert len(llm.requests) == 1
    assert await scalar(db, "SELECT level FROM claim_groups") == "N4"
    invs = await rows(
        db, "SELECT level, depth, stage, requested_by FROM investigations ORDER BY id"
    )
    assert [tuple(i) for i in invs] == [
        ("N3", "standard", "framing", "system:factcheck"),
        ("N4", "reinforced", "framing", "staff:7"),
    ]
    assert (
        await scalar(db, "SELECT count(*) FROM events WHERE topic = 'investigation.requested'") == 2
    )


async def test_n4_promotion_restarts_after_a_failed_n4_investigation(
    db: Database, settings: Settings
) -> None:
    cid = await make_content(db, "2031", POST)
    runtime.set_override(
        "llm", ScriptedLLM(output(cid, [claim("Le chômage est de 7 %.", entities=["INSEE"])]))
    )
    await run_extract(db, settings, cid, "N4")
    async with db.transaction() as s:
        await s.execute(text("UPDATE investigations SET stage = 'failed'"))
    await run_extract(db, settings, cid, "N4")
    assert await scalar(db, "SELECT count(*) FROM investigations") == 2


async def test_extract_with_fake_provider_through_the_router(db: Database) -> None:
    fake_settings = make_settings(llm_fake=True)
    await run_seed(fake_settings)
    cid = await make_content(db, "2040", "Le chômage est passé à 7,5 % en France cette année.")
    await extract(ctx(db, fake_settings, names.JOB_EXTRACT, {"content_id": cid, "level": "N3"}))
    row = (await rows(db, "SELECT claim_type, value_low, jurisdictions FROM claims"))[0]
    assert row[0] == "statistic" and float(row[1]) == 7.5 and row[2] == ["FR"]
    assert await scalar(db, "SELECT count(*) FROM investigations") == 1
    assert await scalar(db, "SELECT count(*) FROM llm_calls WHERE status = 'succeeded'") == 1


# ----------------------------------------------------------------- manual request


def platform_item(ext: str = "3001", text_: str = POST) -> PlatformItem:
    return PlatformItem(
        platform="x",
        external_id=ext,
        text=text_,
        author_external_id="8000",
        author_handle="author",
        lang_hint="fr",
        created_at=NOW - timedelta(hours=2),
        metrics=Metrics(values={"impressions": 900}, measured_at=NOW),
    )


async def run_request(db: Database, settings: Settings, ext: str = "3001") -> None:
    await request(
        ctx(
            db,
            settings,
            names.JOB_REQUEST,
            {"platform": "x", "external_id": ext, "requested_by": "staff:9"},
        )
    )


async def set_x_budget(db: Database, cap: int, spent: int = 0) -> None:
    async with db.transaction() as s:
        await s.execute(
            text(
                "INSERT INTO budget_counters (scope, period_start, period_end, cap_usd_micros,"
                " spent_usd_micros) VALUES ('provider:x', date_trunc('month', now())::date,"
                " (date_trunc('month', now()) + interval '1 month')::date, :c, :s)"
            ),
            {"c": cap, "s": spent},
        )


async def test_request_ingests_and_enqueues_an_n4_extraction_end_to_end(
    db: Database, settings: Settings
) -> None:
    await set_x_budget(db, 1_000_000)
    client = FakePlatform(platform_item())
    runtime.set_override("platform:x", client)

    await run_request(db, settings)

    assert client.calls == ["3001"]
    assert await scalar(db, "SELECT source FROM observations") == "manual"
    spent = await scalar(
        db, "SELECT spent_usd_micros FROM budget_counters WHERE scope = 'provider:x'"
    )
    reserved = await scalar(
        db, "SELECT reserved_usd_micros FROM budget_counters WHERE scope = 'provider:x'"
    )
    assert (spent, reserved) == (5_000, 0)
    cid = await scalar(db, "SELECT id FROM content_items WHERE external_id = '3001'")
    queued = await jobs_of(db, names.JOB_EXTRACT)
    assert queued == [
        {
            "content_id": cid,
            "level": "N4",
            "requested_by": "staff:9",
            "_dedupe": names.extract_key(cid, "N4"),
        }
    ]
    await run_request(db, settings)  # asking twice does not duplicate the job
    assert len(await jobs_of(db, names.JOB_EXTRACT)) == 1

    runtime.set_override(
        "llm", ScriptedLLM(output(cid, [claim("Le chômage est de 7 %.", entities=["INSEE"])]))
    )
    await run_extract(db, settings, cid, "N4", by="staff:9")
    inv = (await rows(db, "SELECT level, depth, requested_by FROM investigations"))[0]
    assert tuple(inv) == ("N4", "reinforced", "staff:9")
    assert await scalar(db, "SELECT reach FROM claim_groups") == 900


async def test_request_outcomes(db: Database, settings: Settings) -> None:
    # Not configured (no override, no bearer token).
    with pytest.raises(PermanentError):
        await run_request(db, settings)
    # Not found.
    runtime.set_override("platform:x", FakePlatform(None))
    with pytest.raises(PermanentError):
        await run_request(db, settings)
    # Rate limited / unavailable → RetryLater; other platform error → retried as is.
    runtime.set_override(
        "platform:x", FakePlatform(error=PlatformRateLimited("429", retry_after=120))
    )
    with pytest.raises(RetryLater) as info:
        await run_request(db, settings)
    assert info.value.delay == 120
    runtime.set_override("platform:x", FakePlatform(error=PlatformUnavailable("503", status=503)))
    with pytest.raises(RetryLater):
        await run_request(db, settings)
    runtime.set_override("platform:x", FakePlatform(error=PlatformBadResponse("odd", status=400)))
    with pytest.raises(PlatformBadResponse):
        await run_request(db, settings)
    assert await scalar(db, "SELECT count(*) FROM content_items") == 0
    assert await scalar(db, "SELECT count(*) FROM jobs WHERE kind = 'factcheck.extract'") == 0


async def test_request_budget_refused(db: Database, settings: Settings) -> None:
    await set_x_budget(db, cap=1_000)  # below the 5 000 estimate
    client = FakePlatform(platform_item())
    runtime.set_override("platform:x", client)
    with pytest.raises(RetryLater) as info:
        await run_request(db, settings)
    assert info.value.delay >= 60
    assert client.calls == []  # no platform call without budget
    assert await scalar(db, "SELECT count(*) FROM content_items") == 0


async def test_request_for_known_content_still_enqueues(db: Database, settings: Settings) -> None:
    cid = await make_content(db, "3002", POST)
    runtime.set_override("platform:x", FakePlatform(platform_item("3002")))
    await run_request(db, settings, "3002")
    assert [q["content_id"] for q in await jobs_of(db, names.JOB_EXTRACT)] == [cid]


async def test_verdict_published_enqueues_translation(db: Database) -> None:
    cid = await make_content(db, "3010", POST)
    async with db.transaction() as s:
        inv = await seed_investigation(s, cid, stage="assembled")
    public, vid, version = (await rows(db, "SELECT public_id, id, version FROM verdicts"))[0]
    async with db.transaction() as s:
        await on_verdict_published(
            s,
            [
                event("verdict.published", {"verdict_id": str(public), "retranslate_from": "fr"}),
                event("verdict.published", {"verdict_id": str(uuid.uuid4())}, 2),
                event("verdict.published", {}, 3),
            ],
        )
    queued = await jobs_of(db, names.JOB_TRANSLATE)
    assert queued == [
        {
            "verdict_id": str(public),
            "retranslate_from": "fr",
            "_dedupe": names.translate_key(vid, version),
        }
    ]
    assert inv.id > 0
