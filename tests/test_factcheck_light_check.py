"""E5 light check: deterministic band path (no model), evidence path (fake Atlas, scripted
model), dossier mode, no evidence, failures and idempotence. Real PostgreSQL."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from tower.core.adapters.knowledge.atlas import AtlasDocument, AtlasRequest, AtlasResult
from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain.factcheck.datamark import unmark
from tower.core.domain.factcheck.values import FactOrigin, LightSignal
from tower.core.domain.security import Actor, Role
from tower.core.factcheck import names, runtime
from tower.core.factcheck.inputs import find_forbidden_keys
from tower.core.factcheck.profiles import invalidate_profile_cache
from tower.core.factcheck.schemas import LightCheckOutput
from tower.core.jobs.queue import PermanentError, RetryLater
from tower.core.llm.errors import AllRefused, BudgetExhausted, ProviderUnavailable
from tower.core.models.factcheck import Claim, EvidencePassage, Investigation, Source
from tower.core.usecases import references as refs_uc
from tower.services.factcheck.light_check import light_check

from .test_factcheck_scheduler import make_group
from .test_factcheck_service import ScriptedLLM, ctx, rows, scalar

EDITOR = Actor.staff(1002, frozenset({Role.EDITOR}), Actor.system("test"))
UNAVAILABLE = ProviderUnavailable("down", provider="fake")


# ------------------------------------------------------------------------- doubles


class FakeAtlas:
    def __init__(self, *documents: AtlasDocument, error: Exception | None = None) -> None:
        self.documents = list(documents)
        self.error = error
        self.requests: list[AtlasRequest] = []

    async def collect(self, request: AtlasRequest) -> AtlasResult:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return AtlasResult(documents=list(self.documents), adapters_used=["fake"])


def doc(
    text_: str, *, adapter: str = "factcheck_tools", domain: str = "factcheck.example.org"
) -> AtlasDocument:
    return AtlasDocument(
        adapter=adapter,
        canonical_url=f"https://{domain}/{uuid.uuid4().hex}",
        original_url=f"https://{domain}/x",
        domain=domain,
        tier="secondary",
        lang="fr",
        text=text_,
    )


def verdict(signal: str = "likely_false", ids: tuple[str, ...] = ("E1",)) -> LightCheckOutput:
    return LightCheckOutput(
        signal=LightSignal(signal),
        explanation="Compared with the evidence.",
        evidence_ids=list(ids),
    )


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    runtime.clear_overrides()
    invalidate_profile_cache()
    yield
    runtime.clear_overrides()
    invalidate_profile_cache()


def check_ctx(db: Database, settings: Settings, group_id: int, dossier: int | None = None) -> Any:
    return ctx(
        db,
        settings,
        names.JOB_LIGHT_CHECK,
        {"group_id": group_id, "dossier_investigation_id": dossier},
    )


async def light(db: Database, group_id: int) -> Any:
    return (
        await rows(
            db,
            "SELECT light_signal, light_checked_at, light_evidence FROM claim_groups WHERE id = :g",
            g=group_id,
        )
    )[0]


async def numeric(db: Database, group_id: int, low: str, high: str) -> None:
    """Give the group's claim a percent interval about the unemployment rate (France)."""
    async with db.transaction() as session:
        claim = await session.get(Claim, await _claim_id(db, group_id))
        assert claim is not None
        claim.subject = "Taux de chômage"
        claim.predicate = "unemployment_rate"
        claim.unit = "percent"
        claim.place = "FR"
        claim.value_low = Decimal(low)
        claim.value_high = Decimal(high)


async def _claim_id(db: Database, group_id: int) -> int:
    return int(
        await scalar(
            db, "SELECT representative_claim_id FROM claim_groups WHERE id = :g", g=group_id
        )
    )


async def validated_fact(db: Database, *, validate: bool = True) -> None:
    async with db.transaction() as session:
        fact = await refs_uc.create_reference_fact(
            session,
            EDITOR,
            indicator="fr.unemployment_rate",
            label="Taux de chômage (BIT), France",
            value=Decimal("7.3"),
            unit="%",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 3, 31),
            territory="fr",
            source_url="https://www.insee.fr/serie/001688527",
            source_name="INSEE",
            published_at=date(2026, 5, 15),
            aliases=["Taux de chômage"],
            origin=FactOrigin.MANUAL,
        )
        if validate:
            await refs_uc.validate_reference_fact(session, EDITOR, fact_id=fact.public_id)


# ------------------------------------------------------------------- band path


@pytest.mark.parametrize(
    ("low", "high", "signal"),
    [
        ("7.0", "7.6", "likely_true"),  # contains 7.3
        ("7.5", "7.8", "needs_context"),  # within one tau (0.365)
        ("8.0", "8.2", "likely_false"),  # within three tau
        ("12", "13", "likely_false"),  # beyond
    ],
)
async def test_band_path_decides_without_a_model(
    db: Database, settings: Settings, low: str, high: str, signal: str
) -> None:
    gid, _ = await make_group(db, "10")
    await numeric(db, gid, low, high)
    await validated_fact(db)
    llm = ScriptedLLM(UNAVAILABLE)
    atlas = FakeAtlas()
    runtime.set_override("llm", llm)
    runtime.set_override("atlas", atlas)
    events_before = await scalar(db, "SELECT count(*) FROM events")

    await light_check(check_ctx(db, settings, gid))

    row = await light(db, gid)
    assert row.light_signal == signal and row.light_checked_at is not None
    assert row.light_evidence["evidence"] == [
        {"id": "F1", "kind": "reference_fact", "source": "INSEE"}
    ]
    assert "fr.unemployment_rate" in row.light_evidence["explanation"]
    assert llm.requests == [] and atlas.requests == []
    assert await scalar(db, "SELECT count(*) FROM events") == events_before  # never published


async def test_unvalidated_fact_is_not_used(db: Database, settings: Settings) -> None:
    gid, _ = await make_group(db, "11")
    await numeric(db, gid, "7.0", "7.6")
    await validated_fact(db, validate=False)
    runtime.set_override("llm", ScriptedLLM(verdict("uncertain", ())))
    runtime.set_override("atlas", FakeAtlas())
    await light_check(check_ctx(db, settings, gid))
    assert (await light(db, gid)).light_evidence["evidence"] == []  # no evidence: uncertain


# --------------------------------------------------------------- evidence path


async def test_evidence_path_uses_atlas_without_encyclopedia_or_web(
    db: Database, settings: Settings
) -> None:
    gid, _ = await make_group(db, "20", level="N1")
    atlas = FakeAtlas(
        doc("Claim X: Faux (checked by the desk)"),
        doc("Wikidata record of the subject", adapter="wikidata", domain="wikidata.org"),
    )
    llm = ScriptedLLM(verdict("likely_false", ("E2", "E9")))
    runtime.set_override("atlas", atlas)
    runtime.set_override("llm", llm)

    await light_check(check_ctx(db, settings, gid))

    request = atlas.requests[0]
    assert request.skip_encyclopedia is True and request.max_web_searches == 0
    assert request.fact_checks_only is False and request.max_reference_fetches == 0
    sent = llm.requests[0]
    assert sent.role == names.ROLE_LIGHT_CHECK and sent.level.value == "N1"
    assert sent.budget_scope == "level:N1" and sent.schema is LightCheckOutput
    evidence = sent.input["evidence"]
    assert [(e["id"], e["kind"], e["source"]) for e in evidence] == [
        ("E1", "fact_check", "factcheck.example.org"),
        ("E2", "structured_source", "wikidata.org"),
    ]
    assert unmark(evidence[0]["text"]) == "Claim X: Faux (checked by the desk)"
    assert find_forbidden_keys(sent.input) == []
    row = await light(db, gid)
    assert row.light_signal == "likely_false"
    # Only the evidence ids copied from the input survive (E9 was invented).
    assert row.light_evidence["evidence"] == [
        {"id": "E2", "kind": "structured_source", "source": "wikidata.org"}
    ]
    assert row.light_evidence["explanation"] == "Compared with the evidence."


async def test_evidence_text_is_truncated(db: Database, settings: Settings) -> None:
    gid, _ = await make_group(db, "21")
    runtime.set_override("atlas", FakeAtlas(doc("word " * 400)))
    llm = ScriptedLLM(verdict("uncertain", ()))
    runtime.set_override("llm", llm)
    await light_check(check_ctx(db, settings, gid))
    assert len(unmark(llm.requests[0].input["evidence"][0]["text"])) <= 600


async def test_model_never_sees_the_author_or_the_level_in_its_input(
    db: Database, settings: Settings
) -> None:
    gid, _ = await make_group(db, "22", level="N2", views=40_000)
    runtime.set_override("atlas", FakeAtlas(doc("Some fact-check")))
    llm = ScriptedLLM(verdict())
    runtime.set_override("llm", llm)
    await light_check(check_ctx(db, settings, gid))
    payload = llm.requests[0].input
    assert find_forbidden_keys(payload) == []
    assert set(payload) == {"claim", "post_date", "evidence"}


async def test_no_evidence_means_uncertain_without_a_model_call(
    db: Database, settings: Settings
) -> None:
    gid, _ = await make_group(db, "23")
    llm = ScriptedLLM(UNAVAILABLE)
    runtime.set_override("atlas", FakeAtlas())
    runtime.set_override("llm", llm)
    await light_check(check_ctx(db, settings, gid))
    row = await light(db, gid)
    assert row.light_signal == "uncertain" and row.light_evidence["evidence"] == []
    assert llm.requests == []


async def test_empty_documents_are_ignored(db: Database, settings: Settings) -> None:
    gid, _ = await make_group(db, "24")
    llm = ScriptedLLM(UNAVAILABLE)
    runtime.set_override("atlas", FakeAtlas(doc("   ")))
    runtime.set_override("llm", llm)
    await light_check(check_ctx(db, settings, gid))
    assert (await light(db, gid)).light_signal == "uncertain" and llm.requests == []


# ----------------------------------------------------------------- dossier mode


async def seed_dossier(db: Database, group_id: int, content_id: int) -> int:
    async with db.transaction() as session:
        investigation = Investigation(
            public_id=uuid.uuid4(),
            claim_group_id=group_id,
            content_id=content_id,
            level="N2",
            depth="standard",
            stage="assembled",
            requested_by="test",
        )
        session.add(investigation)
        source = Source(
            canonical_url="https://insee.fr/a",
            original_url="https://insee.fr/a",
            adapter="brave",
            domain="insee.fr",
            tier="primary",
            lang="fr",
        )
        session.add(source)
        await session.flush()
        for label, body, kept in (
            ("P1", "Kept passage with its text.", True),
            ("P2", None, True),  # kept, but the text is gone: the French translation serves
            ("P3", "Uncited passage whose text remains.", False),
            ("P4", None, False),  # nothing left: excluded
        ):
            session.add(
                EvidencePassage(
                    investigation_id=investigation.id,
                    source_id=source.id,
                    label=label,
                    position=int(label[1]),
                    text=body,
                    lang="fr",
                    translation_fr="Traduction du passage." if label == "P2" else None,
                    simhash=int(label[1]),
                    score=1.0 - int(label[1]) / 10,
                    kept=kept,
                )
            )
        return int(investigation.id)


async def test_dossier_mode_reads_the_kept_passages_not_the_atlas(
    db: Database, settings: Settings
) -> None:
    gid, cid = await make_group(db, "30")
    other, _ = await make_group(db, "31", level="N2")
    inv = await seed_dossier(db, other, cid)
    atlas = FakeAtlas(error=RuntimeError("must not be called"))
    llm = ScriptedLLM(verdict("likely_false", ("E1", "E2")))
    runtime.set_override("atlas", atlas)
    runtime.set_override("llm", llm)

    await light_check(check_ctx(db, settings, gid, inv))

    assert atlas.requests == []
    evidence = llm.requests[0].input["evidence"]
    assert [(e["id"], e["kind"], e["source"]) for e in evidence] == [
        ("E1", "dossier_passage", "insee.fr"),
        ("E2", "dossier_passage", "insee.fr"),
        ("E3", "dossier_passage", "insee.fr"),
    ]
    assert unmark(evidence[0]["text"]) == "Kept passage with its text."
    assert unmark(evidence[1]["text"]) == "Traduction du passage."
    row = await light(db, gid)
    assert row.light_signal == "likely_false"
    assert row.light_evidence["dossier_investigation_id"] == inv
    assert [e["id"] for e in row.light_evidence["evidence"]] == ["E1", "E2"]


async def test_dossier_without_passages_is_uncertain(db: Database, settings: Settings) -> None:
    gid, cid = await make_group(db, "32")
    async with db.transaction() as session:
        inv = Investigation(
            public_id=uuid.uuid4(),
            claim_group_id=gid,
            content_id=cid,
            level="N2",
            depth="standard",
            stage="assembled",
            requested_by="test",
        )
        session.add(inv)
        await session.flush()
        inv_id = int(inv.id)
    llm = ScriptedLLM(UNAVAILABLE)
    runtime.set_override("llm", llm)
    await light_check(check_ctx(db, settings, gid, inv_id))
    assert (await light(db, gid)).light_signal == "uncertain" and llm.requests == []


async def test_dossier_mode_still_prefers_the_band(db: Database, settings: Settings) -> None:
    gid, cid = await make_group(db, "33")
    await numeric(db, gid, "7.0", "7.6")
    await validated_fact(db)
    inv = await seed_dossier(db, gid, cid)
    llm = ScriptedLLM(UNAVAILABLE)
    runtime.set_override("llm", llm)
    await light_check(check_ctx(db, settings, gid, inv))
    assert (await light(db, gid)).light_signal == "likely_true" and llm.requests == []


# ---------------------------------------------------------------------- failures


async def stored_codes(db: Database, reporter: Any) -> list[str]:
    await reporter.flush()
    return [r[0] for r in await rows(db, "SELECT code FROM error_reports")]


async def test_model_failure_is_captured_and_leaves_no_signal(
    db: Database, settings: Settings, reporter: Any
) -> None:
    gid, _ = await make_group(db, "40")
    runtime.set_override("atlas", FakeAtlas(doc("A fact-check")))
    runtime.set_override("llm", ScriptedLLM(UNAVAILABLE))
    await light_check(check_ctx(db, settings, gid))
    row = await light(db, gid)
    assert row.light_signal is None and row.light_checked_at is None
    assert "TWR-FCK-041" in await stored_codes(db, reporter)


async def test_refused_model_is_captured(db: Database, settings: Settings, reporter: Any) -> None:
    gid, _ = await make_group(db, "41")
    runtime.set_override("atlas", FakeAtlas(doc("A fact-check")))
    runtime.set_override("llm", ScriptedLLM(AllRefused("refused")))
    await light_check(check_ctx(db, settings, gid))
    assert (await light(db, gid)).light_signal is None
    assert "TWR-FCK-041" in await stored_codes(db, reporter)


async def test_atlas_failure_is_captured(db: Database, settings: Settings, reporter: Any) -> None:
    gid, _ = await make_group(db, "42")
    llm = ScriptedLLM(verdict())
    runtime.set_override("atlas", FakeAtlas(error=RuntimeError("network")))
    runtime.set_override("llm", llm)
    await light_check(check_ctx(db, settings, gid))
    assert (await light(db, gid)).light_signal is None and llm.requests == []
    assert "TWR-FCK-041" in await stored_codes(db, reporter)


async def test_exhausted_model_budget_retries_later(db: Database, settings: Settings) -> None:
    from datetime import UTC, datetime, timedelta

    gid, _ = await make_group(db, "43")
    runtime.set_override("atlas", FakeAtlas(doc("A fact-check")))
    runtime.set_override(
        "llm", ScriptedLLM(BudgetExhausted("global", datetime.now(UTC) + timedelta(hours=2)))
    )
    with pytest.raises(RetryLater):
        await light_check(check_ctx(db, settings, gid))
    assert (await light(db, gid)).light_signal is None


# --------------------------------------------------------------------- idempotence


async def test_second_run_for_the_same_profile_does_nothing(
    db: Database, settings: Settings
) -> None:
    gid, _ = await make_group(db, "50")
    atlas = FakeAtlas(doc("A fact-check"))
    llm = ScriptedLLM(verdict("likely_false"))
    runtime.set_override("atlas", atlas)
    runtime.set_override("llm", llm)
    await light_check(check_ctx(db, settings, gid))
    first = await light(db, gid)
    await light_check(check_ctx(db, settings, gid))
    assert len(llm.requests) == 1 and len(atlas.requests) == 1
    assert (await light(db, gid)).light_checked_at == first.light_checked_at


async def test_new_profile_version_checks_again(db: Database, settings: Settings) -> None:
    gid, _ = await make_group(db, "51")
    llm = ScriptedLLM(verdict("likely_false"))
    runtime.set_override("atlas", FakeAtlas(doc("A fact-check")))
    runtime.set_override("llm", llm)
    await light_check(check_ctx(db, settings, gid))
    async with db.transaction() as session:
        await session.execute(
            text(
                "UPDATE claim_groups SET light_evidence ="
                " light_evidence || CAST('{\"profile_version\": 9}' AS jsonb)"
            )
        )
    await light_check(check_ctx(db, settings, gid))
    assert len(llm.requests) == 2


async def test_dossier_check_follows_a_plain_check(db: Database, settings: Settings) -> None:
    gid, cid = await make_group(db, "52")
    other, _ = await make_group(db, "53", level="N2")
    inv = await seed_dossier(db, other, cid)
    llm = ScriptedLLM(verdict("uncertain", ()), verdict("likely_false", ("E1",)))
    runtime.set_override("atlas", FakeAtlas(doc("A fact-check")))
    runtime.set_override("llm", llm)
    await light_check(check_ctx(db, settings, gid))
    await light_check(check_ctx(db, settings, gid, inv))
    assert len(llm.requests) == 2
    row = await light(db, gid)
    assert row.light_signal == "likely_false"
    assert row.light_evidence["dossier_investigation_id"] == inv


# ------------------------------------------------------------------ skips and input


async def test_merged_group_is_skipped(db: Database, settings: Settings) -> None:
    gid, _ = await make_group(db, "60", state="merged")
    llm = ScriptedLLM(UNAVAILABLE)
    runtime.set_override("atlas", FakeAtlas(doc("A fact-check")))
    runtime.set_override("llm", llm)
    await light_check(check_ctx(db, settings, gid))
    assert (await light(db, gid)).light_signal is None and llm.requests == []


async def test_unknown_group_is_skipped(db: Database, settings: Settings) -> None:
    await light_check(check_ctx(db, settings, 987654))


async def test_bad_payload_is_permanent(db: Database, settings: Settings) -> None:
    for payload in ({}, {"group_id": "x"}, {"group_id": 1, "dossier_investigation_id": "y"}):
        with pytest.raises(PermanentError):
            await light_check(ctx(db, settings, names.JOB_LIGHT_CHECK, payload))
