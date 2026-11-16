"""Manual notes: a draft written by the team, without any model (domain, use case, job,
gate, bot handler)."""

from __future__ import annotations

from typing import Any, cast

import httpx
import pytest
from sqlalchemy import select

from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain import error_codes as ec
from tower.core.domain.errors import InvalidInput, PermissionDenied
from tower.core.domain.factcheck.manual import (
    ManualNote,
    default_context_need,
    validate_manual_note,
)
from tower.core.domain.factcheck.values import ContextNeed, Veracity
from tower.core.domain.security import Actor, Permission, Role
from tower.core.factcheck import names, runtime
from tower.core.jobs.queue import PermanentError
from tower.core.models.events import Event
from tower.core.models.factcheck import Claim, ContextNoteTranslation, Verdict
from tower.core.models.ingestion import ContentItem
from tower.core.usecases import drafts as drafts_uc
from tower.core.usecases import manual_notes as uc
from tower.intercom.gate_client import GateClient
from tower.intercom.handlers import factcheck as fc
from tower.services.factcheck.jobs import manual

from .conftest import TokenFactory
from .test_factcheck_service import FakePlatform, ctx, jobs_of, platform_item, scalar, set_x_budget
from .test_gate_factcheck import AddStaff, h

SERVICE = Actor.service("intercom", frozenset(Permission))
REVIEWER = Actor.staff(1001, frozenset({Role.REVIEWER}), SERVICE)
EDITOR = Actor.staff(1002, frozenset({Role.EDITOR}), SERVICE)
EXT = "1850000000000000777"
URL = f"https://x.com/someone/status/{EXT}"


def note(**overrides: Any) -> ManualNote:
    fields: dict[str, Any] = {
        "claim_text": "Le chômage a doublé en un an.",
        "claim_type": "statistic",
        "veracity": "false",
        "context_types": ["cherry_picking"],
        "note_text": "Le taux est passé de 7,1 % à 7,3 %. https://insee.fr/a",
        "links": ["https://insee.fr/a"],
        "fact_sheet": "Fiche détaillée.",
    }
    fields.update(overrides)
    return validate_manual_note(**fields)


# --- domain --------------------------------------------------------------------------


def test_validate_normalizes_and_defaults() -> None:
    n = note(
        veracity=" FALSE ",
        context_types=["cherry_picking", "", "cherry_picking", "outdated"],
        links=["https://insee.fr/a", " https://insee.fr/a ", ""],
    )
    assert n.veracity is Veracity.FALSE
    assert n.context_need is ContextNeed.NECESSARY
    assert [t.value for t in n.context_types] == ["cherry_picking", "outdated"]
    assert n.links == ("https://insee.fr/a",)
    assert n.note_lang == "fr"
    assert validate_manual_note(**n.to_payload()) == n  # job payload round trip


def test_default_context_need() -> None:
    assert default_context_need(Veracity.TRUE) is ContextNeed.NONE
    assert default_context_need(Veracity.MOSTLY_TRUE) is ContextNeed.USEFUL
    assert default_context_need(Veracity.MISLEADING) is ContextNeed.NECESSARY
    assert default_context_need(Veracity.UNVERIFIABLE) is ContextNeed.USEFUL


def test_true_without_note_and_unverifiable_without_note_are_accepted() -> None:
    assert note(veracity="true", note_text=None, links=[], fact_sheet=None).note_text is None
    unverifiable = note(veracity="unverifiable", note_text=None, links=[], fact_sheet=None)
    assert unverifiable.context_need is ContextNeed.USEFUL


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"claim_text": "  "}, "claim_text"),
        ({"claim_text": "x" * 301}, "claim_text"),
        ({"claim_type": "rumour"}, "claim_type"),
        ({"veracity": "fake"}, "veracity"),
        ({"context_need": "maybe"}, "context_need"),
        ({"context_types": ["vibes"]}, "context_types"),
        ({"note_lang": "de"}, "note_lang"),
        ({"note_text": "x" * 281}, "note_text"),
        ({"fact_sheet": "x" * 4001}, "fact_sheet"),
        ({"links": ["ftp://a.b"]}, "links"),
        ({"links": [f"https://a.b/{i}" for i in range(11)]}, "links"),
        ({"note_text": None, "links": [], "fact_sheet": None}, "note_text"),
        ({"note_text": None, "fact_sheet": None}, "links"),
        ({"note_text": None, "links": []}, "fact_sheet"),
    ],
)
def test_validate_refuses(overrides: dict[str, Any], field: str) -> None:
    with pytest.raises(InvalidInput) as err:
        note(**overrides)
    assert err.value.code == ec.FCK_MANUAL_INVALID.code
    assert err.value.details["field"] == field


def test_note_length_excludes_links() -> None:
    long_link = "https://example.org/" + "a" * 400
    assert note(note_text="x" * 280 + " " + long_link, links=[]).note_text is not None


def test_key_depends_on_author_post_and_content() -> None:
    n = note()
    assert n.key("x", EXT, "a") == n.key("x", EXT, "a")
    assert n.key("x", EXT, "a") != n.key("x", EXT, "b")
    assert n.key("x", EXT, "a") != n.key("x", "1", "a")
    assert n.key("x", EXT, "a") != note(veracity="misleading").key("x", EXT, "a")


# --- use case (database) -------------------------------------------------------------


async def add_content(db: Database, ext: str = EXT) -> int:
    async with db.transaction() as s:
        content = ContentItem(
            platform="x",
            external_id=ext,
            text="Le chômage a doublé en un an",
            lang="fr",
            lang_confidence=0.99,
            useful_words=6,
            content_hash=bytes(32),
            simhash=1,
            metrics={"impressions": 1200},
        )
        s.add(content)
        await s.flush()
        return content.id


async def topics(db: Database) -> list[str]:
    async with db.session() as s:
        return list(await s.scalars(select(Event.topic).order_by(Event.id)))


async def test_known_post_opens_a_draft_reviewed_then_published(db: Database) -> None:
    await add_content(db)
    async with db.transaction() as s:
        result = await uc.submit_manual_note(s, REVIEWER, note(), url=URL)
    assert (result.status, result.duplicate, result.external_id) == ("drafted", False, EXT)
    assert result.draft_id is not None
    assert await topics(db) == ["case.opened", "draft.ready"]
    assert await scalar(db, "SELECT level FROM claim_groups") == "N4"
    assert await scalar(db, "SELECT reach FROM claim_groups") == 1200
    assert await scalar(db, "SELECT status FROM content_extractions") == "extracted"
    assert await scalar(db, "SELECT count(*) FROM investigations") == 0
    assert await scalar(db, "SELECT count(*) FROM llm_calls") == 0
    async with db.session() as s:
        claim = await s.scalar(select(Claim))
        verdict = await s.scalar(select(Verdict))
        tr = await s.scalar(select(ContextNoteTranslation))
    assert claim is not None and claim.prompt_version == "manual"
    assert verdict is not None and verdict.status == "draft" and verdict.confidence is None
    assert verdict.flags["manual"] is True and verdict.flags["author"] == REVIEWER.label
    assert tr is not None and (tr.origin, tr.fact_sheet) == ("human", "Fiche détaillée.")

    # Same note again: same draft, nothing new.
    async with db.transaction() as s:
        again = await uc.submit_manual_note(s, REVIEWER, note(), platform="x", external_id=EXT)
    assert (again.duplicate, again.draft_id) == (True, result.draft_id)
    assert await scalar(db, "SELECT count(*) FROM verdicts") == 1

    # Not public before validation; an editor validates it like any draft.
    async with db.transaction() as s:
        draft = await drafts_uc.get_draft(s, EDITOR, verdict.public_id)
        assert draft.flags["manual"] is True
        await drafts_uc.review_draft(s, EDITOR, verdict.public_id, decision="accept")
    async with db.session() as s:
        public = await drafts_uc.get_public_verdict(s, Actor.anonymous(), "x", EXT, "fr")
    [claim_out] = public.claims
    assert (claim_out.text, claim_out.veracity) == ("Le chômage a doublé en un an.", "false")
    assert claim_out.note is not None and claim_out.note.links == ["https://insee.fr/a"]
    assert claim_out.note.machine_translated is False
    async with db.session() as s:
        tr = await s.scalar(select(ContextNoteTranslation))
    assert tr is not None and (tr.origin, tr.validated_by) == ("human", EDITOR.label)


async def test_unknown_post_queues_the_job_once(db: Database) -> None:
    async with db.transaction() as s:
        first = await uc.submit_manual_note(s, REVIEWER, note(), url=URL)
        second = await uc.submit_manual_note(s, REVIEWER, note(), url=URL)
    assert (first.status, first.duplicate, first.draft_id) == ("queued", False, None)
    assert second.duplicate is True
    [job] = await jobs_of(db, names.JOB_MANUAL)
    assert job["platform"] == "x" and job["external_id"] == EXT
    assert job["author"] == REVIEWER.label and job["note"] == note().to_payload()


async def test_permissions_and_bad_post(db: Database) -> None:
    viewer = Actor.staff(1003, frozenset(), SERVICE)
    async with db.transaction() as s:
        with pytest.raises(PermissionDenied):
            await uc.submit_manual_note(s, viewer, note(), url=URL)
        with pytest.raises(InvalidInput):
            await uc.submit_manual_note(s, REVIEWER, note())
        with pytest.raises(InvalidInput):
            await uc.submit_manual_note(s, REVIEWER, note(), url="https://example.com/a")


async def test_a_second_note_on_the_same_post_adds_a_claim(db: Database) -> None:
    await add_content(db)
    async with db.transaction() as s:
        await uc.submit_manual_note(s, REVIEWER, note(), url=URL)
        await uc.submit_manual_note(
            s, REVIEWER, note(claim_text="Autre affirmation.", veracity="misleading"), url=URL
        )
    assert await scalar(db, "SELECT count(*) FROM verdicts") == 2
    assert await scalar(db, "SELECT max(position) FROM content_claims") == 1


# --- job ------------------------------------------------------------------------------


def job_payload() -> dict[str, Any]:
    n = note()
    return {
        "platform": "x",
        "external_id": "3001",
        "author": "staff:1001@intercom",
        "key": n.key("x", "3001", "staff:1001@intercom"),
        "note": n.to_payload(),
    }


async def test_job_reads_the_post_then_opens_the_draft(db: Database, settings: Settings) -> None:
    await set_x_budget(db, 1_000_000)
    client = FakePlatform(platform_item())
    runtime.set_override("platform:x", client)
    await manual(ctx(db, settings, names.JOB_MANUAL, job_payload()))
    await manual(ctx(db, settings, names.JOB_MANUAL, job_payload()))  # replay: same draft
    assert client.calls == ["3001"]  # the second run finds the post in the database
    assert await scalar(db, "SELECT source FROM observations") == "manual"
    assert await scalar(db, "SELECT count(*) FROM verdicts WHERE status = 'draft'") == 1
    assert await scalar(db, "SELECT count(*) FROM llm_calls") == 0
    # The automatic extraction would find claims and skip the content.
    assert await scalar(db, "SELECT count(*) FROM content_claims") == 1


async def test_job_failures(db: Database, settings: Settings) -> None:
    with pytest.raises(PermanentError):
        await manual(ctx(db, settings, names.JOB_MANUAL, {"platform": "x"}))
    bad = job_payload()
    bad["note"]["veracity"] = "nope"
    with pytest.raises(PermanentError):
        await manual(ctx(db, settings, names.JOB_MANUAL, bad))
    await set_x_budget(db, 1_000_000)
    runtime.set_override("platform:x", FakePlatform(None))
    with pytest.raises(PermanentError):
        await manual(ctx(db, settings, names.JOB_MANUAL, job_payload()))


# --- gate -----------------------------------------------------------------------------


@pytest.fixture
async def bot(make_token: TokenFactory, add_staff: AddStaff) -> str:
    token = await make_token("intercom")
    await add_staff(1001, ["reviewer"])
    return token


async def test_gate_manual_note(client: httpx.AsyncClient, bot: str, db: Database) -> None:
    await add_content(db)
    body = {
        "url": URL,
        "claim_text": "Le chômage a doublé en un an.",
        "claim_type": "statistic",
        "veracity": "false",
        "note_text": "Le taux est passé de 7,1 % à 7,3 %.",
    }
    r = await client.post("/v1/factcheck/manual", json=body, headers=h(bot, 1001))
    assert r.status_code == 202
    out = r.json()
    assert (out["status"], out["duplicate"], out["external_id"]) == ("drafted", False, EXT)
    r = await client.post(
        "/v1/factcheck/manual", json={**body, "veracity": "nope"}, headers=h(bot, 1001)
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "TWR-FCK-049"
    assert r.json()["error"]["details"]["field"] == "veracity"
    r = await client.post("/v1/factcheck/manual", json=body)
    assert r.status_code in (401, 403)


# --- bot handler (fake gate) ---------------------------------------------------------


class NoteGate:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.bodies: list[dict[str, Any]] = []

    async def submit_manual_note(self, acting: int, body: dict[str, Any]) -> dict[str, Any]:
        self.bodies.append(body)
        return self.result


async def test_handler_sends_lists_and_renders_outcomes() -> None:
    gate = NoteGate(
        {"platform": "x", "external_id": EXT, "status": "drafted", "draft_id": "abcdef12-0"}
    )
    reply = await fc.manual_note(
        cast(GateClient, gate),
        42,
        "fr",
        url=f" {URL} ",
        claim_type="statistic",
        veracity="false",
        claim_text="Le chômage a doublé.",
        context_types="cherry_picking, outdated",
        note_text="Note.",
        links="https://a.b/1\n\nhttps://a.b/2\n",
        fact_sheet="",
    )
    assert reply.accent == "success" and reply.title == "Note enregistrée"
    [body] = gate.bodies
    assert body["url"] == URL
    assert body["context_types"] == ["cherry_picking", "outdated"]
    assert body["links"] == ["https://a.b/1", "https://a.b/2"]
    assert body["fact_sheet"] is None and body["context_need"] is None

    queued = NoteGate({"platform": "x", "external_id": EXT, "status": "queued"})
    reply = await fc.manual_note(
        cast(GateClient, queued), 42, "en", url=URL, claim_type="quote", veracity="true",
        claim_text="c",
    )  # fmt: skip
    assert "read from the platform first" in reply.body

    dup = NoteGate({"platform": "x", "external_id": EXT, "status": "drafted", "duplicate": True})
    reply = await fc.manual_note(
        cast(GateClient, dup), 42, "fr", url=URL, claim_type="quote", veracity="true",
        claim_text="c",
    )  # fmt: skip
    assert reply.title == "Note déjà enregistrée"


def test_bot_choices_match_the_domain() -> None:
    assert tuple(v.value for v in Veracity) == fc.MANUAL_VERACITIES
    assert len(fc.MANUAL_CLAIM_TYPES) <= 25  # Discord limit of choices
