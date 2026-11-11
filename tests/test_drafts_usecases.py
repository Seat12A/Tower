from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import func, select, text

from tower.core.db.database import Database
from tower.core.domain import error_codes as ec
from tower.core.domain.errors import Conflict, InvalidInput, NotFound, PermissionDenied
from tower.core.domain.factcheck.urls import content_url, parse_content_url
from tower.core.domain.security import Actor, Permission, Role
from tower.core.factcheck import names
from tower.core.models.dossiers import Dossier
from tower.core.models.events import Event
from tower.core.models.factcheck import (
    Citation,
    Claim,
    ClaimGroup,
    ClaimGroupMember,
    ContentClaim,
    ContextNote,
    ContextNoteTranslation,
    DossierClaimGroup,
    EvidencePassage,
    Investigation,
    Review,
    Source,
    Verdict,
)
from tower.core.models.ingestion import ContentItem
from tower.core.models.jobs import Job
from tower.core.usecases import drafts as uc

SERVICE = Actor.service("intercom", frozenset(Permission))
REVIEWER = Actor.staff(1001, frozenset({Role.REVIEWER}), SERVICE)
EDITOR = Actor.staff(1002, frozenset({Role.EDITOR}), SERVICE)
ANON = Actor.anonymous()
EXT = "1850000000000000042"


@dataclass(slots=True)
class Built:
    verdict: uuid.UUID
    verdict_id: int
    group_id: int
    note_id: int
    dossier: uuid.UUID
    dossier_id: int
    content_id: int
    claim_id: int
    passage_id: int


async def build_draft(
    db: Database,
    *,
    ext: str = EXT,
    post_lang: str = "fr",
    drafted: bool = True,
    notified: bool = False,
    veracity: str = "false",
    previous_active: bool = False,
    flags: dict[str, Any] | None = None,
) -> Built:
    """Insert a complete draft (claim, group, investigation, sources, citations, note,
    translations, dossier) the way E9 and E10 leave it."""
    async with db.transaction() as s:
        content = ContentItem(
            platform="x",
            external_id=ext,
            text="Le chômage a doublé en un an",
            lang=post_lang,
            lang_confidence=0.99,
            useful_words=6,
            content_hash=bytes(32),
            simhash=int(ext) % 1000,
        )
        s.add(content)
        claim = Claim(
            lang_original=post_lang,
            text_original="Unemployment doubled in a year"
            if post_lang == "en"
            else "Le chômage a doublé en un an",
            text_fr="Le chômage a doublé en un an",
            claim_type="statistic",
            verifiability=0.9,
            prompt_version="1",
        )
        s.add(claim)
        await s.flush()
        group = ClaimGroup(representative_claim_id=claim.id, level="N3", state="investigating")
        s.add(group)
        await s.flush()
        s.add(ClaimGroupMember(group_id=group.id, claim_id=claim.id))
        s.add(ContentClaim(content_id=content.id, claim_id=claim.id, position=0))
        inv = Investigation(
            claim_group_id=group.id,
            content_id=content.id,
            level="N3",
            depth="standard",
            stage="assembled",
            requested_by="system:factcheck",
        )
        s.add(inv)
        await s.flush()
        source = Source(
            canonical_url=f"https://insee.example/{ext}",
            original_url=f"https://insee.example/{ext}",
            adapter="brave",
            publisher="INSEE",
            domain="insee.example",
            tier="primary",
            title="Unemployment series",
        )
        s.add(source)
        await s.flush()
        passage = EvidencePassage(
            investigation_id=inv.id,
            source_id=source.id,
            label="P1",
            position=0,
            text="Le taux de chômage est resté stable à 7,3 %.",
            lang="fr",
            translation_fr=None,
            simhash=1,
        )
        en_passage = EvidencePassage(
            investigation_id=inv.id,
            source_id=source.id,
            label="P2",
            position=1,
            text="The rate stayed at 7.3%.",
            lang="en",
            translation_fr="Le taux est resté à 7,3 %.",
            simhash=2,
        )
        s.add_all([passage, en_passage])
        await s.flush()
        rationale = [
            {
                "assertion": "Le taux est stable.",
                "citations": [
                    {
                        "passage_id": passage.id,
                        "label": "P1",
                        "quote": "resté stable à 7,3 %",
                        "valid": True,
                    },
                    {
                        "passage_id": en_passage.id,
                        "label": "P2",
                        "quote": "stayed at 7.3%",
                        "valid": True,
                    },
                ],
            }
        ]
        previous_id: int | None = None
        if previous_active:
            old = Verdict(
                claim_group_id=group.id,
                veracity="mostly_true",
                context_need="none",
                status="active",
            )
            s.add(old)
            await s.flush()
            group.active_verdict_id = old.id
            group.state = "verified"
            previous_id = old.id
        verdict = Verdict(
            claim_group_id=group.id,
            investigation_id=inv.id,
            veracity=veracity,
            context_need="necessary",
            context_types=["missing_baseline"],
            confidence=0.8,
            status="draft",
            rationale=rationale,
            not_in_sources=["Le niveau avant 2020"],
            review_points=["Vérifier la période"],
            flags={
                "depth": "standard",
                "confidence_band": "high",
                "objection": {"type": "misread_evidence", "explanation": "x", "label": "P1"},
                "critic": {"problems": []},
                "checks": {"note_length": 10},
                "downgraded": False,
                **(flags or {}),
            },
            drafted_at=func.now() if drafted else None,
            notified_at=func.now() if notified else None,
        )
        s.add(verdict)
        await s.flush()
        note = ContextNote(
            verdict_id=verdict.id,
            claim_group_id=group.id,
            context_type="missing_baseline",
            regime="formulation",
            text="Le taux est stable depuis 2023.",
            lang=post_lang,
            links=[f"https://insee.example/{ext}"],
            status="draft",
        )
        s.add(note)
        await s.flush()
        langs = {"fr", post_lang} | ({"en"} if post_lang == "en" else set())
        for lang in sorted(langs):
            s.add(
                ContextNoteTranslation(
                    note_id=note.id,
                    lang=lang,
                    text=f"note-{lang}",
                    fact_sheet=f"sheet-{lang}",
                    origin="machine",
                )
            )
        dossier = Dossier(
            kind="fact_check",
            status="received",
            x_post_id=int(ext),
            content_id=content.id,
            opened_by="system:factcheck",
        )
        s.add(dossier)
        await s.flush()
        s.add(DossierClaimGroup(dossier_id=dossier.id, claim_group_id=group.id))
        await s.flush()
        assert previous_id is None or previous_id != verdict.id
        return Built(
            verdict=verdict.public_id,
            verdict_id=verdict.id,
            group_id=group.id,
            note_id=note.id,
            dossier=dossier.public_id,
            dossier_id=dossier.id,
            content_id=content.id,
            claim_id=claim.id,
            passage_id=passage.id,
        )


async def events(db: Database, topic: str | None = None) -> list[Event]:
    async with db.session() as s:
        stmt = select(Event).order_by(Event.id)
        if topic:
            stmt = stmt.where(Event.topic == topic)
        return list((await s.scalars(stmt)).all())


async def verdict_row(db: Database, public_id: uuid.UUID) -> Verdict:
    async with db.session() as s:
        return (await s.execute(select(Verdict).where(Verdict.public_id == public_id))).scalar_one()


async def translations(db: Database, note_id: int) -> dict[str, ContextNoteTranslation]:
    async with db.session() as s:
        rows = await s.scalars(
            select(ContextNoteTranslation).where(ContextNoteTranslation.note_id == note_id)
        )
        return {r.lang: r for r in rows}


# --- URL parsing ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://x.com/jdoe/status/1850000000000000042",
        "https://twitter.com/jdoe/status/1850000000000000042?s=20",
        "https://mobile.twitter.com/jdoe/status/1850000000000000042",
        "http://www.x.com/jdoe/status/1850000000000000042/photo/1",
        "https://x.com/i/status/1850000000000000042",
        "x.com/jdoe/status/1850000000000000042",
        "  https://X.com/jdoe/status/1850000000000000042#frag ",
    ],
)
def test_parse_content_url_accepts_x_status_urls(url: str) -> None:
    assert parse_content_url(url) == ("x", "1850000000000000042")


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not a url",
        "https://example.com/jdoe/status/1",
        "https://x.com/jdoe",
        "https://x.com/jdoe/likes/1",
        "https://x.com/jdoe/status/abc",
        "https://x.com/jdoe/status/99999999999999999999",
        "https://x.com.evil.test/jdoe/status/1",
        "ftp://x.com/jdoe/status/1",
        "https://x.com/waytoolonghandle_xx/status/1",
    ],
)
def test_parse_content_url_rejects_other_urls(url: str) -> None:
    with pytest.raises(InvalidInput) as err:
        parse_content_url(url)
    assert err.value.code == ec.FCK_BAD_URL.code


def test_content_url() -> None:
    assert content_url("x", "5") == "https://x.com/i/status/5"
    assert content_url("bluesky", "abc") is None


# --- reading -------------------------------------------------------------------------


async def test_list_pending_and_notified_filter(db: Database) -> None:
    a = await build_draft(db, ext="1850000000000000001")
    b = await build_draft(db, ext="1850000000000000002", notified=True)
    await build_draft(db, ext="1850000000000000003", drafted=False)  # not in the queue
    async with db.session() as s:
        everything = await uc.list_pending_drafts(s, REVIEWER, notified=None, limit=10)
        assert [d.draft_id for d in everything] == [str(b.verdict), str(a.verdict)]
        todo = await uc.list_pending_drafts(s, REVIEWER, notified=False, limit=10)
        assert [d.draft_id for d in todo] == [str(a.verdict)]
        done = await uc.list_pending_drafts(s, REVIEWER, notified=True, limit=10)
        assert [d.draft_id for d in done] == [str(b.verdict)]
        page = await uc.list_pending_drafts(s, REVIEWER, limit=1)
        older = await uc.list_pending_drafts(s, REVIEWER, limit=5, before_id=page[0].seq)
        assert [d.draft_id for d in older] == [str(a.verdict)]
        with pytest.raises(PermissionDenied):
            await uc.list_pending_drafts(s, ANON)


async def test_get_draft_view(db: Database) -> None:
    built = await build_draft(db)
    async with db.session() as s:
        view = await uc.get_draft(s, REVIEWER, built.verdict)
    assert view.draft_id == str(built.verdict)
    assert view.status == "draft" and view.level == "N3" and view.depth == "standard"
    assert view.veracity == "false" and view.context_need == "necessary"
    assert view.confidence == 0.8 and view.confidence_band == "high"
    assert view.claim["text_fr"] == "Le chômage a doublé en un an"
    assert view.content == {
        "platform": "x",
        "external_id": EXT,
        "url": f"https://x.com/i/status/{EXT}",
        "lang": "fr",
    }
    assert view.dossier == {"id": str(built.dossier), "status": "received"}
    assert set(view.notes) == {"fr"} and view.notes["fr"]["origin"] == "machine"
    assert view.links == [f"https://insee.example/{EXT}"]
    cites = view.rationale[0]["citations"]
    assert [c["label"] for c in cites] == ["P1", "P2"]
    assert cites[0]["source"]["publisher"] == "INSEE" and cites[0]["translation_fr"] is None
    assert cites[1]["translation_fr"] == "Le taux est resté à 7,3 %."
    assert len(view.sources) == 1 and view.sources[0]["tier"] == "primary"
    assert view.not_in_sources == ["Le niveau avant 2020"]
    assert view.objection is not None and view.objection["type"] == "misread_evidence"
    assert view.critic == {"problems": []} and view.checks == {"note_length": 10}
    assert view.flags == {"depth": "standard", "downgraded": False}
    assert view.drafted_at is not None and view.notified_at is None


async def test_get_draft_unknown_or_not_queued(db: Database) -> None:
    hidden = await build_draft(db, drafted=False)
    async with db.session() as s:
        for public_id in (uuid.uuid4(), hidden.verdict):
            with pytest.raises(NotFound) as err:
                await uc.get_draft(s, REVIEWER, public_id)
            assert err.value.code == ec.FCK_DRAFT_NOT_FOUND.code
        with pytest.raises(PermissionDenied):
            await uc.get_draft(s, ANON, hidden.verdict)


async def test_mark_notified_is_idempotent_and_silent(db: Database) -> None:
    built = await build_draft(db)
    before = await verdict_row(db, built.verdict)
    for _ in range(2):
        async with db.transaction() as s:
            await uc.mark_draft_notified(s, REVIEWER, built.verdict)
    after = await verdict_row(db, built.verdict)
    assert before.notified_at is None and after.notified_at is not None
    assert after.version == before.version
    assert await events(db) == []
    stamp = after.notified_at
    async with db.transaction() as s:
        await uc.mark_draft_notified(s, REVIEWER, built.verdict)
    assert (await verdict_row(db, built.verdict)).notified_at == stamp
    async with db.transaction() as s:
        with pytest.raises(NotFound):
            await uc.mark_draft_notified(s, REVIEWER, uuid.uuid4())


# --- review --------------------------------------------------------------------------


async def test_accept_by_reviewer_is_an_endorsement(db: Database) -> None:
    built = await build_draft(db)
    async with db.transaction() as s:
        result = await uc.review_draft(s, REVIEWER, built.verdict, decision="accept")
    assert result.outcome == "endorsed" and result.draft.status == "draft"
    assert result.draft.dossier == {"id": str(built.dossier), "status": "in_review"}
    verdict = await verdict_row(db, built.verdict)
    assert verdict.status == "draft" and verdict.validated_at is None
    assert [e.topic for e in await events(db)] == ["dossier.transitioned", "draft.reviewed"]
    payload = (await events(db, "draft.reviewed"))[0].payload
    assert payload["outcome"] == "endorsed" and payload["reviewer"] == REVIEWER.label
    assert payload["dossier_id"] == str(built.dossier)
    # a second reviewer may endorse as well; the dossier is already in review
    other = Actor.staff(1003, frozenset({Role.REVIEWER}), SERVICE)
    async with db.transaction() as s:
        again = await uc.review_draft(s, other, built.verdict, decision="accept")
    assert again.outcome == "endorsed"
    async with db.session() as s:
        rows = (await s.scalars(select(Review).order_by(Review.id))).all()
    assert [r.reviewer for r in rows] == [REVIEWER.label, other.label]
    assert len(await events(db, "dossier.transitioned")) == 1


async def test_edit_by_reviewer_records_the_proposal_only(db: Database) -> None:
    built = await build_draft(db)
    async with db.transaction() as s:
        result = await uc.review_draft(
            s,
            REVIEWER,
            built.verdict,
            decision="edit",
            veracity="misleading",
            note_text="Better note",
            labels=["wrong_veracity"],
        )
    assert result.outcome == "endorsed" and result.draft.veracity == "false"
    async with db.session() as s:
        review = (await s.scalars(select(Review))).one()
    assert review.diff["veracity"] == ["false", "misleading"]
    assert review.diff["note"]["after"] == "Better note"
    assert review.final_veracity == "false" and review.labels == ["wrong_veracity"]
    assert (await translations(db, built.note_id))["fr"].origin == "machine"


async def test_accept_by_editor_validates(db: Database) -> None:
    built = await build_draft(db, previous_active=True, post_lang="en")
    async with db.session() as s:
        old_id = (await s.get(ClaimGroup, built.group_id)).active_verdict_id  # type: ignore[union-attr]
    async with db.transaction() as s:
        result = await uc.review_draft(
            s, EDITOR, built.verdict, decision="accept", labels=["wrong_context", "wrong_context"]
        )
    assert result.outcome == "validated" and result.draft.status == "active"
    assert result.draft.dossier == {"id": str(built.dossier), "status": "validated"}
    verdict = await verdict_row(db, built.verdict)
    assert verdict.status == "active" and verdict.validated_by == EDITOR.label
    assert verdict.validated_at is not None
    async with db.session() as s:
        group = await s.get(ClaimGroup, built.group_id)
        assert group is not None
        assert group.active_verdict_id == built.verdict_id and group.state == "verified"
        old = await s.get(Verdict, old_id)
        assert old is not None and old.status == "superseded"
        note = await s.get(ContextNote, built.note_id)
        assert note is not None and note.status == "validated"
        review = (await s.scalars(select(Review))).one()
        assert review.outcome == "validated" and review.labels == ["wrong_context"]
        assert review.review_seconds is not None and review.review_seconds >= 0
    rows = await translations(db, built.note_id)
    assert {r.origin for r in rows.values()} == {"original"}
    assert all(r.validated_by == EDITOR.label for r in rows.values()) and set(rows) == {"fr", "en"}
    topics = [e.topic for e in await events(db)]
    assert topics == [
        "dossier.transitioned",
        "dossier.transitioned",
        "draft.reviewed",
        "verdict.published",
    ]
    published = (await events(db, "verdict.published"))[0].payload
    assert published == {
        "verdict_id": str(built.verdict),
        "group_id": published["group_id"],
        "dossier_id": str(built.dossier),
        "veracity": "false",
        "context_need": "necessary",
        "platform": "x",
        "external_id": EXT,
        "retranslate_from": None,
    }


async def test_accept_keeps_a_non_reference_language_machine(db: Database) -> None:
    built = await build_draft(db, post_lang="de")
    async with db.transaction() as s:
        await uc.review_draft(s, EDITOR, built.verdict, decision="accept")
    rows = await translations(db, built.note_id)
    assert rows["fr"].origin == "original" and rows["de"].origin == "machine"


async def test_edit_by_editor_changes_veracity_and_note(db: Database) -> None:
    built = await build_draft(db)
    async with db.transaction() as s:
        result = await uc.review_draft(
            s,
            EDITOR,
            built.verdict,
            decision="edit",
            veracity="misleading",
            note_text="  Note corrigée https://insee.example/x  ",
            note_lang="fr",
            reason="tone",
        )
    assert result.outcome == "validated" and result.draft.veracity == "misleading"
    rows = await translations(db, built.note_id)
    assert rows["fr"].text == "Note corrigée https://insee.example/x"
    assert rows["fr"].origin == "human" and rows["fr"].validated_by == EDITOR.label
    published = (await events(db, "verdict.published"))[0].payload
    assert published["retranslate_from"] == "fr" and published["veracity"] == "misleading"
    reviewed = (await events(db, "draft.reviewed"))[0].payload
    assert reviewed["final_veracity"] == "misleading" and reviewed["decision"] == "edit"
    async with db.session() as s:
        review = (await s.scalars(select(Review))).one()
    assert review.diff["veracity"] == ["false", "misleading"]
    assert review.diff["note"]["before"] == "note-fr" and review.reason == "tone"


async def test_edit_in_english_creates_the_row_when_missing(db: Database) -> None:
    built = await build_draft(db)  # post language fr: only the fr version exists
    async with db.transaction() as s:
        await uc.review_draft(
            s, EDITOR, built.verdict, decision="edit", note_text="An English note", note_lang="en"
        )
    rows = await translations(db, built.note_id)
    assert rows["en"].origin == "human" and rows["en"].text == "An English note"
    assert rows["fr"].origin == "original"
    assert (await events(db, "verdict.published"))[0].payload["retranslate_from"] == "en"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"decision": "edit"},
        {"decision": "edit", "veracity": "maybe"},
        {"decision": "edit", "note_text": "x" * 281},
        {"decision": "edit", "note_text": "ok", "note_lang": "de"},
        {"decision": "reject"},
        {"decision": "reject", "reason": "   "},
        {"decision": "accept", "labels": ["not_a_label"]},
        {"decision": "bogus"},
    ],
)
async def test_bad_reviews_are_refused_and_change_nothing(
    db: Database, kwargs: dict[str, Any]
) -> None:
    built = await build_draft(db)
    with pytest.raises(InvalidInput) as err:
        async with db.transaction() as s:
            await uc.review_draft(s, EDITOR, built.verdict, **kwargs)
    assert err.value.code == ec.FCK_BAD_REVIEW.code
    assert (await verdict_row(db, built.verdict)).status == "draft"
    assert await events(db) == []


async def test_note_length_excludes_links(db: Database) -> None:
    built = await build_draft(db)
    link = "https://example.org/" + "a" * 200
    async with db.transaction() as s:
        result = await uc.review_draft(
            s, EDITOR, built.verdict, decision="edit", note_text="y" * 270 + " " + link
        )
    assert result.outcome == "validated"


async def test_reject_by_editor(db: Database) -> None:
    built = await build_draft(db)
    async with db.transaction() as s:
        result = await uc.review_draft(
            s,
            EDITOR,
            built.verdict,
            decision="reject",
            reason="Not checkable",
            labels=["wrong_source"],
        )
    assert result.outcome == "rejected" and result.draft.status == "rejected"
    assert result.draft.dossier == {"id": str(built.dossier), "status": "rejected"}
    async with db.session() as s:
        note = await s.get(ContextNote, built.note_id)
        assert note is not None and note.status == "rejected"
        group = await s.get(ClaimGroup, built.group_id)
        assert group is not None and group.active_verdict_id is None
        review = (await s.scalars(select(Review))).one()
        assert review.outcome == "rejected" and review.reason == "Not checkable"
    assert [e.topic for e in await events(db)] == [
        "dossier.transitioned",
        "dossier.transitioned",
        "draft.reviewed",
    ]


async def test_reject_by_reviewer_is_denied(db: Database) -> None:
    built = await build_draft(db)
    with pytest.raises(PermissionDenied):
        async with db.transaction() as s:
            await uc.review_draft(s, REVIEWER, built.verdict, decision="reject", reason="no")
    with pytest.raises(PermissionDenied):
        async with db.transaction() as s:
            await uc.review_draft(s, ANON, built.verdict, decision="accept")
    assert (await verdict_row(db, built.verdict)).status == "draft"


async def test_already_reviewed_and_unknown(db: Database) -> None:
    built = await build_draft(db)
    async with db.transaction() as s:
        await uc.review_draft(s, EDITOR, built.verdict, decision="accept")
    with pytest.raises(Conflict) as err:
        async with db.transaction() as s:
            await uc.review_draft(s, EDITOR, built.verdict, decision="accept")
    assert err.value.code == ec.FCK_DRAFT_ALREADY_REVIEWED.code
    with pytest.raises(NotFound) as missing:
        async with db.transaction() as s:
            await uc.review_draft(s, EDITOR, uuid.uuid4(), decision="accept")
    assert missing.value.code == ec.FCK_DRAFT_NOT_FOUND.code


async def test_expected_version_conflict(db: Database) -> None:
    built = await build_draft(db)
    with pytest.raises(Conflict) as err:
        async with db.transaction() as s:
            await uc.review_draft(s, EDITOR, built.verdict, decision="accept", expected_version=99)
    assert err.value.code == "TWR-GEN-003"
    async with db.transaction() as s:
        result = await uc.review_draft(
            s, EDITOR, built.verdict, decision="accept", expected_version=1
        )
    assert result.outcome == "validated"


async def test_validation_rolls_back_as_a_whole(db: Database) -> None:
    built = await build_draft(db)
    with pytest.raises(RuntimeError):
        async with db.transaction() as s:
            await uc.review_draft(s, EDITOR, built.verdict, decision="accept")
            raise RuntimeError("boom")
    assert (await verdict_row(db, built.verdict)).status == "draft"
    assert await events(db) == []
    async with db.session() as s:
        dossier = (await s.scalars(select(Dossier))).one()
        assert dossier.status == "received"


# --- manual request ------------------------------------------------------------------


async def test_request_factcheck_by_url_and_dedupe(db: Database) -> None:
    async with db.transaction() as s:
        first = await uc.request_factcheck(s, EDITOR, url=f"https://twitter.com/a/status/{EXT}")
    assert (first.platform, first.external_id, first.duplicate) == ("x", EXT, False)
    assert first.job_id is not None
    async with db.transaction() as s:
        second = await uc.request_factcheck(s, EDITOR, platform="x", external_id=EXT)
    assert second.duplicate and second.job_id is None
    async with db.session() as s:
        job = (await s.scalars(select(Job))).one()
    assert (job.queue, job.kind) == (names.FC_FAST, names.JOB_REQUEST)
    assert job.dedupe_key == names.request_key("x", EXT)
    assert job.payload == {"platform": "x", "external_id": EXT, "requested_by": EDITOR.label}
    assert await events(db) == []


async def test_request_factcheck_errors(db: Database) -> None:
    async with db.transaction() as s:
        with pytest.raises(InvalidInput) as bad_url:
            await uc.request_factcheck(s, EDITOR, url="https://example.com/x")
        assert bad_url.value.code == ec.FCK_BAD_URL.code
        with pytest.raises(InvalidInput):
            await uc.request_factcheck(s, EDITOR, platform="x", external_id="abc")
        with pytest.raises(InvalidInput):
            await uc.request_factcheck(s, EDITOR, platform="nowhere", external_id="1")
        with pytest.raises(InvalidInput):
            await uc.request_factcheck(s, EDITOR)
        with pytest.raises(PermissionDenied):
            await uc.request_factcheck(s, REVIEWER, url=f"https://x.com/a/status/{EXT}")
        with pytest.raises(PermissionDenied):
            await uc.request_factcheck(s, ANON, url=f"https://x.com/a/status/{EXT}")


# --- public verdict ------------------------------------------------------------------


async def test_public_verdict_never_shows_drafts(db: Database) -> None:
    built = await build_draft(db)
    async with db.session() as s:
        with pytest.raises(NotFound) as err:
            await uc.get_public_verdict(s, ANON, "x", EXT, "fr")
        assert err.value.code == ec.FCK_VERDICT_NOT_FOUND.code
        with pytest.raises(NotFound):
            await uc.get_public_verdict(s, ANON, "x", "999", None)
    async with db.transaction() as s:
        await uc.review_draft(s, REVIEWER, built.verdict, decision="accept")  # endorsement only
    async with db.session() as s:
        with pytest.raises(NotFound):
            await uc.get_public_verdict(s, ANON, "x", EXT, None)


async def test_public_verdict_language_fallback_and_machine_flag(db: Database) -> None:
    built = await build_draft(db, post_lang="de")
    async with db.transaction() as s:
        await uc.review_draft(s, EDITOR, built.verdict, decision="accept")
    async with db.session() as s:
        fr = await uc.get_public_verdict(s, ANON, "x", EXT, "fr")
        assert fr.url == f"https://x.com/i/status/{EXT}" and fr.lang == "de"
        claim = fr.claims[0]
        assert claim.veracity == "false" and claim.context_need == "necessary"
        assert claim.context_types == ["missing_baseline"] and claim.validated_at is not None
        assert claim.text == "Le chômage a doublé en un an"
        assert claim.note is not None
        assert (claim.note.lang, claim.note.text, claim.note.machine_translated) == (
            "fr",
            "note-fr",
            False,
        )
        assert claim.note.fact_sheet == "sheet-fr" and claim.note.links
        # the post language (machine translation, flagged) when the requested one is missing
        other = await uc.get_public_verdict(s, ANON, "x", EXT, "es-ES")
        assert other.claims[0].note is not None
        assert other.claims[0].note.lang == "de" and other.claims[0].note.machine_translated
        assert other.claims[0].text == "Le chômage a doublé en un an"  # claim original (de)
        default = await uc.get_public_verdict(s, ANON, "x", EXT, None)
        assert default.claims[0].note is not None and default.claims[0].note.lang == "de"


async def test_public_verdict_falls_back_to_english_then_french(db: Database) -> None:
    built = await build_draft(db, post_lang="en")
    async with db.transaction() as s:
        await uc.review_draft(s, EDITOR, built.verdict, decision="accept")
        await s.execute(
            text("DELETE FROM context_note_translations WHERE note_id = :n AND lang = 'en'"),
            {"n": built.note_id},
        )
    async with db.session() as s:
        # post language gone: French is the last resort
        got = await uc.get_public_verdict(s, ANON, "x", EXT, "it")
        assert got.claims[0].note is not None and got.claims[0].note.lang == "fr"
    async with db.transaction() as s:
        s.add(
            ContextNoteTranslation(
                note_id=built.note_id, lang="en", text="en-text", origin="machine"
            )
        )
    async with db.session() as s:
        got = await uc.get_public_verdict(s, ANON, "x", EXT, "it")
        assert got.claims[0].note is not None
        assert got.claims[0].note.lang == "en" and got.claims[0].note.machine_translated
        assert got.claims[0].text == "Unemployment doubled in a year"


async def test_public_verdict_after_replacement_shows_only_the_active_one(db: Database) -> None:
    built = await build_draft(db, previous_active=True)
    async with db.transaction() as s:
        await uc.review_draft(s, EDITOR, built.verdict, decision="edit", veracity="true")
    async with db.session() as s:
        got = await uc.get_public_verdict(s, ANON, "x", EXT, "fr")
    assert [c.veracity for c in got.claims] == ["true"]
    async with db.session() as s:
        count = await s.scalar(
            select(func.count()).select_from(Verdict).where(Verdict.status == "active")
        )
        assert count == 1
        assert await s.scalar(select(func.count()).select_from(Citation)) == 0
