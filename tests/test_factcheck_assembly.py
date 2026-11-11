"""E10 assembly, draft opening, quota, translations and the periodic tasks."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text

from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain.errors import Conflict
from tower.core.domain.factcheck.confidence import ConfidenceSignals, compute_confidence
from tower.core.domain.factcheck.values import Agreement
from tower.core.domain.llm import TokenUsage
from tower.core.domain.security import Actor
from tower.core.factcheck import names, runtime
from tower.core.factcheck.schemas import TranslateOutput
from tower.core.models.factcheck import Investigation, SourceCacheEntry, Verdict
from tower.core.ports.llm import LLMRequest, LLMResult
from tower.core.usecases.assembly import open_draft, store_machine_translations
from tower.services.factcheck.jobs import assemble, translate
from tower.services.factcheck.periodic import drain_quota, purge_source_cache

from .conftest import make_settings
from .test_factcheck_service import (
    POST,
    FakePlatform,
    ctx,
    make_content,
    platform_item,
    rows,
    scalar,
    seed_investigation,
)

SYSTEM = Actor.system("test")


@pytest.fixture(autouse=True)
def _reset_runtime() -> Iterator[None]:
    runtime.clear_overrides()
    yield
    runtime.clear_overrides()


async def run_assemble(db: Database, settings: Settings, investigation_id: int) -> None:
    await assemble(ctx(db, settings, names.JOB_ASSEMBLE, {"investigation_id": investigation_id}))


async def seed(db: Database, content_id: int, **kw: Any) -> Any:
    async with db.transaction() as s:
        inv = await seed_investigation(s, content_id, **kw)
        return inv


async def verdict_row(db: Database, investigation_id: int) -> Any:
    return (
        await rows(
            db,
            "SELECT confidence, flags, drafted_at, version FROM verdicts"
            " WHERE investigation_id = :i",
            i=investigation_id,
        )
    )[0]


async def stage(db: Database, investigation_id: int) -> str:
    return str(
        await scalar(db, "SELECT stage FROM investigations WHERE id = :i", i=investigation_id)
    )


# -------------------------------------------------------------------------- assemble


async def test_confidence_is_computed_from_valid_citations(
    db: Database, settings: Settings
) -> None:
    cid = await make_content(db, "4001", POST)
    inv = await seed(
        db,
        cid,
        agreement="identical",
        verdict_flags={
            "conflicting_sources": False,
            "second_pass": "concordant",
            "primary_passages": ["P1"],
        },
        cited=[
            ("P1", "insee.fr|primary", "INSEE", 0),
            ("P2", "lemonde.fr|secondary", "Le Monde", 2**40),
        ],
    )
    await run_assemble(db, settings, inv.id)

    expected = compute_confidence(
        ConfidenceSignals(
            primary_direct=True,
            independent_origins=2,
            agreement=Agreement.IDENTICAL,
            second_pass=True,
        )
    )
    confidence, flags, drafted_at, _ = await verdict_row(db, inv.id)
    assert confidence == expected == 0.9  # capped
    assert flags["confidence_band"] == "high"
    assert drafted_at is not None
    assert await stage(db, inv.id) == "assembled"


async def test_weak_evidence_gives_a_medium_confidence_and_keeps_judge_flags(
    db: Database, settings: Settings
) -> None:
    cid = await make_content(db, "4002", POST)
    inv = await seed(
        db,
        cid,
        verdict_flags={"agreement": "style", "has_primary": False, "round": 2},
        cited=[("P1", "blog.example|tertiary", None, 5)],
    )
    await run_assemble(db, settings, inv.id)
    confidence, flags, _, _ = await verdict_row(db, inv.id)
    assert confidence == compute_confidence(
        ConfidenceSignals(independent_origins=1, agreement=Agreement.STYLE)
    )
    assert flags["confidence_band"] == "medium" and flags["round"] == 2


async def test_recheck_flags(db: Database, settings: Settings) -> None:
    async def check(ext: str, client: Any) -> dict[str, Any]:
        cid = await make_content(db, ext, POST)
        inv = await seed(db, cid)
        if client is None:
            runtime.set_override("platform:x", None)
        else:
            runtime.set_override("platform:x", client)
        await run_assemble(db, settings, inv.id)
        return dict((await verdict_row(db, inv.id))[1])

    unchanged = await check("4010", FakePlatform(platform_item("4010")))
    assert unchanged["recheck"] == "ok" and "post_deleted" not in unchanged
    assert "post_modified" not in unchanged

    deleted = await check("4011", FakePlatform(None))
    assert deleted["post_deleted"] is True

    modified = await check(
        "4012", FakePlatform(platform_item("4012", "Texte tout à fait différent."))
    )
    assert modified["post_modified"] is True

    skipped = await check("4013", None)  # no token, no override
    assert skipped["recheck"] == "skipped"

    from tower.core.ports.platforms import PlatformUnavailable

    failed = await check("4014", FakePlatform(error=PlatformUnavailable("down", status=503)))
    assert failed["recheck"] == "failed"
    # A failed recheck never blocks the draft.
    assert await scalar(db, "SELECT count(*) FROM verdicts WHERE drafted_at IS NOT NULL") == 5


async def test_assemble_opens_the_dossier_and_publishes_draft_ready(
    db: Database, settings: Settings
) -> None:
    cid = await make_content(db, "4020", POST, metrics={"impressions": 10})
    inv = await seed(db, cid, level="N4", veracity="false")
    runtime.set_override("platform:x", FakePlatform(platform_item("4020")))
    await run_assemble(db, settings, inv.id)

    dossier = (
        await rows(
            db,
            "SELECT id, public_id, kind, status, x_post_id, content_id, title, priority"
            " FROM dossiers",
        )
    )[0]
    assert dossier[2:] == ("fact_check", "received", 4020, cid, "Le chômage est de 7 %.", 50)
    assert (
        await scalar(
            db, "SELECT count(*) FROM dossier_claim_groups WHERE dossier_id = :d", d=dossier[0]
        )
        == 1
    )
    verdict_public, group_public = (
        await rows(
            db,
            "SELECT v.public_id, g.public_id FROM verdicts v JOIN claim_groups g"
            " ON g.id = v.claim_group_id",
        )
    )[0]
    ready = (await rows(db, "SELECT payload, xid FROM events WHERE topic = 'draft.ready'"))[0]
    opened_xid = await scalar(db, "SELECT xid FROM events WHERE topic = 'dossier.opened'")
    confidence = (await verdict_row(db, inv.id))[0]
    assert ready[0] == {
        "draft_id": str(verdict_public),
        "dossier_id": str(dossier[1]),
        "group_id": str(group_public),
        "level": "N4",
        "veracity": "false",
        "context_need": "none",
        "confidence": confidence,
        "platform": "x",
        "external_id": "4020",
        "url": "https://x.com/i/status/4020",
    }
    assert ready[1] == opened_xid  # one transaction
    assert await stage(db, inv.id) == "assembled"


async def test_n3_draft_has_priority_zero_and_title_is_truncated(
    db: Database, settings: Settings
) -> None:
    cid = await make_content(db, "4021", POST)
    inv = await seed(db, cid)
    async with db.transaction() as s:
        await s.execute(text("UPDATE claims SET text_fr = :t"), {"t": "é" * 250})
    runtime.set_override("platform:x", FakePlatform(platform_item("4021")))
    await run_assemble(db, settings, inv.id)
    title, priority = (await rows(db, "SELECT title, priority FROM dossiers"))[0]
    assert priority == 0 and title == "é" * 200


async def test_assemble_is_a_noop_unless_the_investigation_is_completed(
    db: Database, settings: Settings
) -> None:
    cid = await make_content(db, "4030", POST)
    inv = await seed(db, cid, stage="judging")
    await run_assemble(db, settings, inv.id)
    await run_assemble(db, settings, 987654)
    assert await stage(db, inv.id) == "judging"
    assert await scalar(db, "SELECT count(*) FROM dossiers") == 0
    done = await seed(db, await make_content(db, "4031", POST), stage="completed")
    runtime.set_override("platform:x", FakePlatform(platform_item("4031")))
    await run_assemble(db, settings, done.id)
    await run_assemble(db, settings, done.id)  # second run: already assembled
    assert await scalar(db, "SELECT count(*) FROM dossiers") == 1
    assert await scalar(db, "SELECT count(*) FROM events WHERE topic = 'draft.ready'") == 1


async def test_unverifiable_at_framing_is_assembled_without_a_dossier(
    db: Database, settings: Settings
) -> None:
    cid = await make_content(db, "4040", POST)
    inv = await seed(db, cid, with_verdict=False, flags={"unverifiable_at_framing": True})
    await run_assemble(db, settings, inv.id)
    assert await stage(db, inv.id) == "assembled"
    assert await scalar(db, "SELECT count(*) FROM dossiers") == 0
    assert await scalar(db, "SELECT count(*) FROM events WHERE topic = 'draft.ready'") == 0


async def test_quota_reached_leaves_the_draft_waiting_then_drain_opens_it(
    db: Database,
) -> None:
    settings = make_settings(fc_draft_daily_quota=1)
    runtime.set_override("platform:x", FakePlatform(platform_item("4050")))
    first = await seed(db, await make_content(db, "4050", POST))
    second = await seed(db, await make_content(db, "4051", POST))
    await run_assemble(db, settings, first.id)
    await run_assemble(db, settings, second.id)

    assert (await verdict_row(db, first.id))[2] is not None
    confidence, flags, drafted_at, _ = await verdict_row(db, second.id)
    assert drafted_at is None and "over_quota_since" in flags
    assert confidence is not None and flags["confidence_band"] in ("low", "medium", "high")
    assert await stage(db, second.id) == "assembled"
    assert await scalar(db, "SELECT count(*) FROM dossiers") == 1

    async with db.transaction() as s:
        assert await drain_quota(s, settings) == 0  # no slot left today
    bigger = make_settings(fc_draft_daily_quota=2)
    async with db.transaction() as s:
        assert await drain_quota(s, bigger) == 1
    assert (await verdict_row(db, second.id))[2] is not None
    async with db.transaction() as s:
        assert await drain_quota(s, bigger) == 0
    assert await scalar(db, "SELECT count(*) FROM events WHERE topic = 'draft.ready'") == 2


async def test_yesterdays_drafts_do_not_count_in_todays_quota(db: Database) -> None:
    settings = make_settings(fc_draft_daily_quota=1)
    runtime.set_override("platform:x", FakePlatform(platform_item("4060")))
    old = await seed(db, await make_content(db, "4060", POST))
    async with db.transaction() as s:
        await s.execute(
            text("UPDATE verdicts SET drafted_at = :d WHERE investigation_id = :i"),
            {"d": datetime.now(UTC) - timedelta(days=1, minutes=1), "i": old.id},
        )
    new = await seed(db, await make_content(db, "4061", POST))
    runtime.set_override("platform:x", FakePlatform(platform_item("4061")))
    await run_assemble(db, settings, new.id)
    assert (await verdict_row(db, new.id))[2] is not None


async def test_drain_opens_drafts_by_priority(db: Database) -> None:
    runtime.set_override("platform:x", None)
    settings = make_settings(fc_draft_daily_quota=0)
    plain_true = await seed(
        db,
        await make_content(db, "4070", POST),
        veracity="true",
        verdict_flags={"agreement": "identical"},
    )
    plain_false = await seed(
        db,
        await make_content(db, "4071", POST),
        veracity="false",
        verdict_flags={"agreement": "identical"},
    )
    n4 = await seed(
        db,
        await make_content(db, "4072", POST),
        level="N4",
        veracity="true",
        verdict_flags={"agreement": "identical"},
    )
    for inv in (plain_true, plain_false, n4):
        await run_assemble(db, settings, inv.id)  # quota 0: all wait
    assert await scalar(db, "SELECT count(*) FROM verdicts WHERE drafted_at IS NULL") == 3
    # Make every confidence high so only level and veracity decide.
    async with db.transaction() as s:
        await s.execute(text("UPDATE verdicts SET confidence = 0.9"))

    async def opened() -> set[int]:
        return {
            r[0]
            for r in await rows(
                db, "SELECT investigation_id FROM verdicts WHERE drafted_at IS NOT NULL"
            )
        }

    async with db.transaction() as s:
        assert await drain_quota(s, make_settings(fc_draft_daily_quota=1)) == 1
    assert await opened() == {n4.id}
    async with db.transaction() as s:
        assert await drain_quota(s, make_settings(fc_draft_daily_quota=2)) == 1
    assert await opened() == {n4.id, plain_false.id}
    async with db.transaction() as s:
        assert await drain_quota(s, make_settings(fc_draft_daily_quota=10)) == 1
    assert await opened() == {n4.id, plain_false.id, plain_true.id}


async def test_open_draft_twice_conflicts(db: Database) -> None:
    cid = await make_content(db, "4080", POST)
    await seed(db, cid, stage="assembled")
    async with db.transaction() as s:
        verdict = (await s.scalars(select(Verdict))).one()
        investigation = (await s.scalars(select(Investigation))).one()
        await open_draft(s, SYSTEM, verdict, investigation)
        with pytest.raises(Conflict):
            await open_draft(s, SYSTEM, verdict, investigation)


# ------------------------------------------------------------------------ translations


class TranslatingLLM:
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        data = request.input
        return LLMResult(
            output=TranslateOutput(
                lang=data["target_lang"],
                text=f"{data['kind']}:{data['target_lang']}:{data['text']}",
            ),
            provider="fake",
            model="fake",
            usage=TokenUsage(),
            cost_usd_micros=0,
            free_tier=False,
            cache_hit=False,
            request_id="r",
        )


async def seed_note(
    db: Database, cid: int, *, note_lang: str, translations: dict[str, tuple[str, str, str | None]]
) -> tuple[str, int]:
    """Active verdict with a note; ``translations``: lang → (origin, text, fact sheet)."""
    inv = await seed(db, cid, stage="assembled", verdict_status="active")
    async with db.transaction() as s:
        await s.execute(
            text(
                "INSERT INTO context_notes (verdict_id, claim_group_id, regime, text, lang, status)"
                " SELECT v.id, v.claim_group_id, 'formulation', 'note', :l, 'draft' FROM verdicts v"
            ),
            {"l": note_lang},
        )
        note_id = (await s.execute(text("SELECT id FROM context_notes"))).scalar_one()
        for lang, (origin, body, sheet) in translations.items():
            await s.execute(
                text(
                    "INSERT INTO context_note_translations"
                    " (note_id, lang, text, fact_sheet, origin)"
                    " VALUES (:n, :l, :t, :f, :o)"
                ),
                {"n": note_id, "l": lang, "t": body, "f": sheet, "o": origin},
            )
    public = await scalar(
        db, "SELECT public_id FROM verdicts WHERE investigation_id = :i", i=inv.id
    )
    return str(public), int(note_id)


async def translations(db: Database) -> dict[str, tuple[str, str, str | None]]:
    return {
        r[0]: (r[1], r[2], r[3])
        for r in await rows(
            db, "SELECT lang, origin, text, fact_sheet FROM context_note_translations"
        )
    }


async def run_translate(
    db: Database, settings: Settings, verdict: str, retranslate_from: str | None = None
) -> None:
    await translate(
        ctx(
            db,
            settings,
            names.JOB_TRANSLATE,
            {"verdict_id": verdict, "retranslate_from": retranslate_from},
        )
    )


async def test_translate_fills_missing_targets_from_the_reviewed_row(
    db: Database, settings: Settings
) -> None:
    llm = TranslatingLLM()
    runtime.set_override("llm", llm)
    cid = await make_content(db, "4100", POST)
    verdict, _ = await seed_note(
        db,
        cid,
        note_lang="de",
        translations={
            "fr": ("original", "Note FR", "Fiche FR"),
            "de": ("machine", "alte Maschine", None),
        },
    )
    await run_translate(db, settings, verdict)
    got = await translations(db)
    assert got["fr"] == ("original", "Note FR", "Fiche FR")  # untouched
    assert got["de"] == ("machine", "alte Maschine", None)  # exists: not retranslated
    assert got["en"] == ("machine", "context_note:en:Note FR", "fact_sheet:en:Fiche FR")
    kinds = [
        (r.input["kind"], r.input["source_lang"], r.input["target_lang"]) for r in llm.requests
    ]
    assert kinds == [("context_note", "fr", "en"), ("fact_sheet", "fr", "en")]
    assert all(r.role == names.ROLE_TRANSLATE and r.level.value == "N3" for r in llm.requests)
    await run_translate(db, settings, verdict)  # nothing left to do
    assert len(llm.requests) == 2


async def test_retranslate_replaces_machine_rows_but_never_human_or_original(
    db: Database, settings: Settings
) -> None:
    llm = TranslatingLLM()
    runtime.set_override("llm", llm)
    cid = await make_content(db, "4101", POST)
    verdict, _ = await seed_note(
        db,
        cid,
        note_lang="de",
        translations={
            "fr": ("human", "Note FR corrigée", None),
            "en": ("original", "Original EN", None),
            "de": ("machine", "alte Maschine", "alte Fiche"),
        },
    )
    await run_translate(db, settings, verdict, retranslate_from="fr")
    got = await translations(db)
    assert got["fr"] == ("human", "Note FR corrigée", None)
    assert got["en"] == ("original", "Original EN", None)
    assert got["de"] == ("machine", "context_note:de:Note FR corrigée", None)
    assert [r.input["source_lang"] for r in llm.requests] == ["fr"]


async def test_best_reviewed_row_prefers_human_then_french(
    db: Database, settings: Settings
) -> None:
    llm = TranslatingLLM()
    runtime.set_override("llm", llm)
    cid = await make_content(db, "4102", POST)
    verdict, _ = await seed_note(
        db,
        cid,
        note_lang="en",
        translations={"en": ("human", "Human EN", None), "fr": ("original", "Original FR", None)},
    )
    await run_translate(db, settings, verdict)
    assert await translations(db) == {
        "en": ("human", "Human EN", None),
        "fr": ("original", "Original FR", None),
    }  # fr and en exist already: nothing to translate
    assert llm.requests == []


async def test_translate_skips_inactive_verdicts_and_unknown_ids(
    db: Database, settings: Settings
) -> None:
    llm = TranslatingLLM()
    runtime.set_override("llm", llm)
    cid = await make_content(db, "4103", POST)
    verdict, _ = await seed_note(
        db, cid, note_lang="de", translations={"fr": ("original", "FR", None)}
    )
    async with db.transaction() as s:
        await s.execute(text("UPDATE verdicts SET status = 'superseded'"))
    await run_translate(db, settings, verdict)
    await run_translate(db, settings, "00000000-0000-0000-0000-000000000000")
    assert llm.requests == []


async def test_translation_failure_is_retried(db: Database, settings: Settings) -> None:
    class Failing:
        async def complete(self, request: LLMRequest) -> LLMResult:
            raise ConnectionError("boom")

    runtime.set_override("llm", Failing())
    cid = await make_content(db, "4104", POST)
    verdict, _ = await seed_note(
        db, cid, note_lang="fr", translations={"fr": ("original", "FR", None)}
    )
    with pytest.raises(ConnectionError):
        await run_translate(db, settings, verdict)
    assert list(await translations(db)) == ["fr"]


async def test_store_machine_translations_never_overwrites_reviewed_rows(db: Database) -> None:
    cid = await make_content(db, "4105", POST)
    _, note_id = await seed_note(
        db,
        cid,
        note_lang="fr",
        translations={
            "fr": ("original", "FR", None),
            "en": ("human", "EN", None),
            "de": ("machine", "old", None),
        },
    )
    async with db.transaction() as s:
        written = await store_machine_translations(
            s,
            SYSTEM,
            note_id=note_id,
            items=[
                ("fr", "x", None),
                ("en", "y", None),
                ("de", "new", "sheet"),
                ("es", "nuevo", None),
            ],
        )
    assert written == 2
    assert await translations(db) == {
        "fr": ("original", "FR", None),
        "en": ("human", "EN", None),
        "de": ("machine", "new", "sheet"),
        "es": ("machine", "nuevo", None),
    }


# ----------------------------------------------------------------------------- periodic


async def test_purge_source_cache_deletes_only_expired_rows(
    db: Database, settings: Settings
) -> None:
    now = datetime.now(UTC)
    async with db.transaction() as s:
        s.add(
            SourceCacheEntry(
                key=b"a" * 32, adapter="x", response={}, expires_at=now - timedelta(hours=1)
            )
        )
        s.add(
            SourceCacheEntry(
                key=b"b" * 32, adapter="x", response={}, expires_at=now + timedelta(hours=1)
            )
        )
    async with db.transaction() as s:
        assert await purge_source_cache(s, settings) == 1
    assert await scalar(db, "SELECT count(*) FROM source_cache") == 1
