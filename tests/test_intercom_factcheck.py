from __future__ import annotations

import asyncio
import re
from typing import Any, cast

import pytest

from tower.core.config import Settings
from tower.intercom.gate_client import GateClient, GateError, GateUnavailable
from tower.intercom.handlers import MAX_MESSAGE, Reply
from tower.intercom.handlers import factcheck as fc
from tower.intercom.i18n import en, fr
from tower.intercom.notifier import TOPIC_CHANNELS, format_event

from .test_intercom_notifier import FakeGate, ev, run_until

DRAFT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
URL_RE = re.compile(r"<(https?://[^>\s]+)>")


def make_view(**overrides: Any) -> dict[str, Any]:
    view: dict[str, Any] = {
        "draft_id": DRAFT,
        "seq": 7,
        "version": 1,
        "status": "draft",
        "level": "N3",
        "depth": "reinforced",
        "veracity": "false",
        "context_need": "necessary",
        "context_types": ["missing_baseline"],
        "confidence": 0.8,
        "confidence_band": "high",
        "claim": {
            "text_original": "Unemployment doubled in a year",
            "lang": "en",
            "text_fr": "Le chômage a doublé en un an",
        },
        "content": {
            "platform": "x",
            "external_id": "1850000000000000042",
            "url": "https://x.com/i/status/1850000000000000042",
            "lang": "en",
        },
        "case": {"id": "d", "status": "received"},
        "notes": {
            "fr": {
                "text": "Le taux est stable.\nDepuis 2023.",
                "fact_sheet": None,
                "origin": "machine",
            },
            "en": {"text": "The rate is stable.", "fact_sheet": None, "origin": "machine"},
        },
        "links": [],
        "rationale": [
            {
                "assertion": "Le taux est stable.",
                "citations": [
                    {
                        "label": "P1",
                        "quote": "stayed at 7.3%",
                        "valid": True,
                        "lang": "en",
                        "translation_fr": "est resté à 7,3 %",
                        "source": {
                            "url": "https://insee.example/a",
                            "publisher": "INSEE",
                            "title": "t",
                            "tier": "primary",
                        },
                    },
                    {
                        "label": "P9",
                        "quote": "dropped quote",
                        "valid": False,
                        "lang": "fr",
                        "translation_fr": None,
                        "source": None,
                    },
                ],
            }
        ],
        "not_in_sources": ["Le niveau avant 2020"],
        "review_points": ["Vérifier la période", "Les sources ne sont pas indépendantes"],
        "objection": {
            "type": "not_independent",
            "direction": "less_severe",
            "explanation": "Les sources ne sont pas indépendantes",
            "label": "P1",
        },
        "critic": None,
        "checks": {},
        "flags": {},
        "sources": [],
        "drafted_at": "2026-11-11T10:00:00",
        "notified_at": None,
    }
    view.update(overrides)
    return view


def cards(reply: Reply) -> list[Reply]:
    return [reply, *reply.extra]


def joined(reply: Reply) -> str:
    return "\n\n".join(c.text for c in cards(reply))


# --- rendering -----------------------------------------------------------------------


def test_render_draft_french_blocks_in_order() -> None:
    text = joined(fc.render_draft(make_view(), "fr"))
    assert "Brouillon de fact-check" in text
    for detail in (
        "Verdict: `Faux`",
        "Contexte: `Contexte nécessaire`",
        "Confiance: `80% (élevée)`",
        "Niveau: `N3 · renforcée`",
    ):
        assert detail in text, detail
    assert "Le chômage a doublé en un an" in text and "(FR)" not in text
    assert "Post : <https://x.com/i/status/1850000000000000042>" in text
    assert "**Note proposée** (fr)\n> Le taux est stable.\n> Depuis 2023." in text
    assert '[P1] "stayed at 7.3%" (INSEE <https://insee.example/a>)' in text.replace(
        "« ", '"'
    ).replace(" »", '"')
    assert "Traduction : est resté à 7,3 %" in text  # French locale, English passage
    assert "dropped quote" not in text  # invalid citations never displayed
    order = [
        "Brouillon de fact-check",
        "**Affirmation**",
        "**Note proposée**",
        "**Justification**",
        "**Ce que les sources ne disent pas**",
        "- Le niveau avant 2020",
        "**À vérifier en priorité**",
    ]
    positions = [text.index(marker) for marker in order]
    assert positions == sorted(positions)


def test_render_draft_objection_is_highlighted_and_not_repeated() -> None:
    text = joined(fc.render_draft(make_view(), "fr"))
    assert (
        "> **OBJECTION** (sources non indépendantes, moins sévère) : Les sources ne sont pas"
        in text
    )
    assert text.count("Les sources ne sont pas indépendantes") == 1
    assert "- Vérifier la période" in text
    # the objection comes first in the block, before the judge's points
    assert text.index("OBJECTION") < text.index("- Vérifier la période")
    no_objection = joined(fc.render_draft(make_view(objection=None), "en"))
    assert "OBJECTION" not in no_objection
    assert "- Les sources ne sont pas indépendantes" in no_objection


def test_render_draft_english_uses_english_texts_and_no_french_translation() -> None:
    text = joined(fc.render_draft(make_view(), "en"))
    assert "Verdict: `False`" in text and "Confidence: `80% (high)`" in text
    assert "Level: `N3 · reinforced`" in text
    assert "**Claim**\nUnemployment doubled in a year" in text
    assert "**Proposed note** (en)\n> The rate is stable." in text
    assert "Translation" not in text and "7,3" not in text
    assert "**To check first**" in text and "**What the sources do not say**" in text


def test_render_draft_english_falls_back_to_french() -> None:
    view = make_view(
        claim={
            "text_original": "Le chômage a doublé",
            "lang": "de",
            "text_fr": "Le chômage a doublé",
        },
        notes={"fr": {"text": "Note FR", "fact_sheet": None, "origin": "machine"}},
    )
    text = joined(fc.render_draft(view, "en"))
    assert "**Claim** (FR)\nLe chômage a doublé" in text
    assert "**Proposed note** (fr)\n> Note FR" in text
    none = joined(fc.render_draft(make_view(notes={}), "en"))
    assert "No note proposed." in none


def test_render_draft_warnings() -> None:
    view = make_view(
        flags={
            "downgraded": True,
            "post_deleted": True,
            "post_modified": True,
            "budget_incomplete": True,
            "cap_reached": True,
            "refused": ["judge", "critic"],
            "conflicting_sources": True,
        },
        checks={
            "invalid_quotes": ["1", "2"],
            "loaded_terms": [{"term": "scandale", "lang": "fr"}],
            "unknown_links": ["https://x.example"],
        },
        critic={
            "contains_opinion": True,
            "contains_generalization": True,
            "judges_author": True,
            "loaded_words": ["honteux"],
            "unsourced_facts": ["a"],
            "comments": "Trop affirmatif",
        },
    )
    text = joined(fc.render_draft(view, "fr"))
    assert "**Avertissements**" in text
    for needle in (
        "rétrogradé",
        "supprimé",
        "modifié",
        "Budget épuisé",
        "Plafond",
        "judge, critic",
        "se contredisent",
        "2 citation(s)",
        "scandale",
        "1 lien(s)",
        "une opinion",
        "généralisation",
        "juge l'auteur",
        "honteux",
        "1 fait(s) sans source",
        "Trop affirmatif",
    ):
        assert needle in text, needle
    assert "Avertissements" not in joined(fc.render_draft(make_view(), "fr"))


def test_render_draft_splits_without_cutting_urls() -> None:
    citations: list[dict[str, Any]] = [
        {
            "label": f"P{i}",
            "quote": "q" * 250,
            "valid": True,
            "lang": "fr",
            "translation_fr": None,
            "source": {
                "url": f"https://example.org/{'path/' * 15}{i}",
                "publisher": "Pub",
                "title": None,
                "tier": "primary",
            },
        }
        for i in range(30)
    ]
    view = make_view(rationale=[{"assertion": "A" * 300, "citations": citations}])
    reply = fc.render_draft(view, "fr")
    parts = [c.body for c in cards(reply)]
    assert 1 < len(parts) <= 10
    assert all(0 < len(p) <= MAX_MESSAGE for p in parts)
    # the controls are on the last card only
    assert [bool(c.buttons) for c in cards(reply)] == [False] * (len(parts) - 1) + [True]
    assert cards(reply)[0].title.endswith(f"(1/{len(parts)})")
    found = {m for p in parts for m in URL_RE.findall(p)}
    expected = {c["source"]["url"] for c in citations} | {
        "https://x.com/i/status/1850000000000000042"
    }
    # parts are capped: every URL that is displayed is complete
    assert found <= expected and len(found) >= 10
    # nothing was cut inside a "<url>" wrapper
    assert all(p.count("<http") == len(URL_RE.findall(p)) for p in parts)


def test_split_text() -> None:
    url = "https://example.org/" + "a" * 60
    text = ("word " * 30 + url + " ") * 5
    pieces = fc.split_text(text, 100)
    assert all(len(p) <= 100 for p in pieces)
    assert sum(p.count(url) for p in pieces) == 5  # no URL cut
    assert fc.split_text("short", 100) == ["short"]
    assert fc.split_text("", 100) == []
    lines = "\n".join(f"line {i}" for i in range(100))
    out = fc.split_text(lines, 50)
    assert all(len(p) <= 50 for p in out) and "\n".join(out).count("line") == 100
    assert fc.split_text("x" * 250, 100) == ["x" * 100, "x" * 100, "x" * 50]  # one huge token


def test_confidence_missing_is_shown_as_unknown() -> None:
    text = joined(fc.render_draft(make_view(confidence=None, confidence_band=None), "en"))
    assert "Confidence: `?`" in text


# --- vocabulary ----------------------------------------------------------------------


def test_custom_ids_round_trip() -> None:
    for action in fc.ACTIONS:
        assert fc.custom_id(action, DRAFT) == f"t12a:fc:{action}:{DRAFT}"
        assert fc.parse_custom_id(fc.custom_id(action, DRAFT)) == (action, DRAFT)
        # messages posted before the redesign keep the legacy id
        assert fc.parse_custom_id(f"fc:{action}:{DRAFT}") == (action, DRAFT)
    assert fc.parse_custom_id("fc:nuke:" + DRAFT) is None
    assert fc.parse_custom_id("fc:validate:short") is None
    assert len(fc.custom_id("correct", DRAFT)) <= 100


@pytest.mark.parametrize(
    ("locale", "typed", "expected"),
    [
        ("fr", "Plutôt vrai", "mostly_true"),
        ("fr", "plutot vrai", "mostly_true"),
        ("fr", "FAUX", "false"),
        ("fr", "trompeur", "misleading"),
        ("fr", "non vérifiable", "unverifiable"),
        ("en", "Mostly true", "mostly_true"),
        ("en", "mostly_true", "mostly_true"),
        ("en", "vrai", "true"),  # either language is understood
        ("en", "  ", None),
        ("en", "maybe", None),
    ],
)
def test_parse_veracity(locale: str, typed: str, expected: str | None) -> None:
    assert fc.parse_veracity(locale, typed) == expected


def test_vocabulary_labels() -> None:
    assert [fc.veracity_label("fr", v) for v in fc.VERACITIES] == [
        "Vrai",
        "Plutôt vrai",
        "Trompeur",
        "Faux",
        "Non vérifiable",
    ]
    assert [fc.veracity_label("en", v) for v in fc.VERACITIES] == [
        "True",
        "Mostly true",
        "Misleading",
        "False",
        "Unverifiable",
    ]
    assert fr.MESSAGES["context_need.none"] == "Aucun besoin"
    assert en.MESSAGES["context_need.necessary"] == "Context necessary"
    for catalog in (fr.MESSAGES, en.MESSAGES):
        for key in (
            "draft.modal.correct.title",
            "draft.modal.veracity.label",
            "draft.modal.note.label",
        ):
            assert len(catalog[key]) <= 45  # Discord limit of titles and labels


# --- handlers (fake gate) ------------------------------------------------------------


class ReviewGate:
    def __init__(self, result: dict[str, Any] | Exception) -> None:
        self.result = result
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def review_draft(
        self, acting: int | None, draft_id: str, **kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append(("review", (acting, draft_id), kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def request_factcheck(self, acting: int | None, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("request", (acting,), kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def gate_of(fake: ReviewGate) -> GateClient:
    return cast(GateClient, fake)


async def test_review_accept_outcomes() -> None:
    validated = ReviewGate({"outcome": "validated", "draft": {"veracity": "false"}})
    reply = await fc.review_accept(gate_of(validated), 42, "fr", DRAFT)
    assert reply.final and reply.accent == "success" and reply.ephemeral
    assert reply.title == "Brouillon validé"
    assert "Brouillon `aaaaaaaa` validé : le verdict est maintenant Faux." in reply.body
    assert validated.calls == [("review", (42, DRAFT), {"decision": "accept"})]
    endorsed = ReviewGate({"outcome": "endorsed", "draft": {"veracity": "false"}})
    reply = await fc.review_accept(gate_of(endorsed), 42, "en", DRAFT)
    assert "approval" in reply.text and reply.ephemeral and not reply.final


async def test_review_errors_are_translated() -> None:
    gate = ReviewGate(GateError(409, "TWR-FCK-008", "The draft was already decided"))
    reply = await fc.review_accept(gate_of(gate), 42, "fr", DRAFT)
    assert reply.ephemeral and "déjà été tranché" in reply.text and "TWR-FCK-008" in reply.text
    gate = ReviewGate(GateError(403, "TWR-AUTH-005", "Missing permission"))
    reply = await fc.review_reject(gate_of(gate), 42, "en", DRAFT, "because")
    assert "permission" in reply.text and "TWR-AUTH-005" in reply.text
    gate = ReviewGate(GateError(422, "TWR-FCK-009", "bad"))
    reply = await fc.review_edit(gate_of(gate), 42, "fr", DRAFT, None, None, None)
    assert "TWR-FCK-009" in reply.text and "Relecture invalide" in reply.text
    gate = ReviewGate(GateUnavailable(0, "TWR-BOT-001", "down"))
    reply = await fc.review_accept(gate_of(gate), 42, "en", DRAFT)
    assert "TWR-BOT-001" in reply.text


async def test_review_edit_sends_the_member_language_and_clean_values() -> None:
    gate = ReviewGate({"outcome": "validated", "draft": {"veracity": "misleading"}})
    reply = await fc.review_edit(
        gate_of(gate), 42, "en", DRAFT, " Mostly true ", "  A better note  ", "  tone  "
    )
    assert reply.final and "Misleading" in reply.text
    assert gate.calls[0][2] == {
        "decision": "edit",
        "veracity": "mostly_true",
        "note_text": "A better note",
        "note_lang": "en",
        "reason": "tone",
    }
    gate = ReviewGate({"outcome": "endorsed", "draft": {"veracity": "false"}})
    await fc.review_edit(gate_of(gate), 42, "fr", DRAFT, "", "Une note", None)
    assert gate.calls[0][2]["note_lang"] == "fr" and gate.calls[0][2]["veracity"] is None
    await fc.review_edit(gate_of(gate), 42, "fr", DRAFT, "Faux", "", "")
    assert gate.calls[1][2]["note_lang"] is None and gate.calls[1][2]["note_text"] is None


async def test_review_edit_unknown_veracity_never_reaches_gate() -> None:
    gate = ReviewGate({"outcome": "validated", "draft": {}})
    reply = await fc.review_edit(gate_of(gate), 42, "fr", DRAFT, "peut-être", None, None)
    assert "Véracité inconnue" in reply.text and "Plutôt vrai" in reply.text and not gate.calls
    assert reply.ephemeral and not reply.final


async def test_review_reject() -> None:
    gate = ReviewGate({"outcome": "rejected", "draft": {"veracity": "false"}})
    reply = await fc.review_reject(gate_of(gate), 42, "en", DRAFT, " not checkable ")
    assert reply.final and "rejected" in reply.text
    assert gate.calls[0][2] == {"decision": "reject", "reason": "not checkable"}


async def test_factcheck_request_replies() -> None:
    gate = ReviewGate({"platform": "x", "external_id": "123", "accepted": True, "duplicate": False})
    reply = await fc.request(gate_of(gate), 42, "en", " https://x.com/a/status/123 ")
    assert "requested" in reply.text and "`123`" in reply.text and reply.ephemeral
    assert gate.calls[0] == ("request", (42,), {"url": "https://x.com/a/status/123"})
    gate = ReviewGate({"platform": "x", "external_id": "123", "accepted": True, "duplicate": True})
    assert "déjà en file" in (await fc.request(gate_of(gate), 42, "fr", "u")).text
    gate = ReviewGate(GateError(422, "TWR-FCK-012", "bad url"))
    reply = await fc.request(gate_of(gate), 42, "fr", "nope")
    assert "TWR-FCK-012" in reply.text and "Lien non pris en charge" in reply.text


# --- reconciliation ------------------------------------------------------------------


class ListGate:
    def __init__(self, drafts: list[dict[str, Any]]) -> None:
        self.drafts = sorted(drafts, key=lambda d: -d["seq"])  # newest first like gate
        self.list_calls: list[dict[str, Any]] = []
        self.notified: list[str] = []
        self.fail_mark = False

    async def list_drafts(
        self,
        acting: int | None,
        *,
        notified: bool | None = None,
        limit: int = 25,
        before_id: int | None = None,
    ) -> list[dict[str, Any]]:
        self.list_calls.append(
            {"acting": acting, "notified": notified, "limit": limit, "before": before_id}
        )
        rows = [d for d in self.drafts if before_id is None or d["seq"] < before_id]
        return rows[:limit]

    async def mark_draft_notified(self, acting: int | None, draft_id: str) -> None:
        if self.fail_mark:
            raise GateUnavailable(0, "TWR-BOT-001", "down")
        self.notified.append(draft_id)


async def test_reconcile_posts_everything_oldest_first_across_pages() -> None:
    gate = ListGate([make_view(draft_id=f"id-{n}", seq=n) for n in range(1, 8)])
    posted: list[str] = []

    async def post(view: dict[str, Any]) -> bool:
        posted.append(view["draft_id"])
        return True

    count = await fc.reconcile_drafts(cast(GateClient, gate), post, page_size=3)
    assert count == 7 and posted == [f"id-{n}" for n in range(1, 8)]
    assert [c["before"] for c in gate.list_calls] == [None, 5, 2]
    assert all(c["notified"] is False and c["acting"] is None for c in gate.list_calls)


async def test_reconcile_is_bounded_and_survives_a_failing_post() -> None:
    gate = ListGate([make_view(draft_id=f"id-{n}", seq=n) for n in range(1, 30)])
    posted: list[str] = []

    async def post(view: dict[str, Any]) -> bool:
        if view["draft_id"] == "id-27":
            raise RuntimeError("discord down")
        posted.append(view["draft_id"])
        return True

    count = await fc.reconcile_drafts(cast(GateClient, gate), post, page_size=5, max_pages=2)
    assert len(gate.list_calls) == 2 and count == 9  # 10 read, one failed
    assert "id-27" not in posted and posted[0] == "id-20"
    assert await fc.reconcile_drafts(cast(GateClient, ListGate([])), post) == 0


async def test_post_draft_marks_notified_only_when_posted() -> None:
    gate = ListGate([])
    sent: list[tuple[str, Reply, str]] = []

    async def send(channel: str, reply: Reply, draft_id: str) -> bool:
        sent.append((channel, reply, draft_id))
        return True

    assert await fc.post_draft(cast(GateClient, gate), send, make_view(), "fr")
    assert gate.notified == [DRAFT] and sent[0][0] == "cases" and sent[0][2] == DRAFT

    async def not_posted(channel: str, reply: Reply, draft_id: str) -> bool:
        return False

    gate.notified.clear()
    assert not await fc.post_draft(cast(GateClient, gate), not_posted, make_view(), "fr")
    assert gate.notified == []
    gate.fail_mark = True  # posted but not marked: still reported as posted
    assert await fc.post_draft(cast(GateClient, gate), send, make_view(), "fr")


# --- notifier ------------------------------------------------------------------------


class DraftGate(FakeGate):
    def __init__(self, scripts: list[Any], drafts: list[dict[str, Any]], **kwargs: Any) -> None:
        super().__init__(scripts, **kwargs)
        self.drafts = {d["draft_id"]: d for d in drafts}
        self.pending = list(drafts)
        self.notified: list[str] = []
        self.fetch_error: Exception | None = None
        self.order: list[str] = []

    async def get_draft(self, acting: int | None, draft_id: str) -> dict[str, Any]:
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.drafts[draft_id]

    async def list_drafts(self, acting: int | None, **kwargs: Any) -> list[dict[str, Any]]:
        self.order.append("list")
        return [d for d in self.pending if not d.get("notified_at") and d["status"] == "draft"]

    async def mark_draft_notified(self, acting: int | None, draft_id: str) -> None:
        self.notified.append(draft_id)
        self.drafts[draft_id]["notified_at"] = "now"

    async def stream_events(self, **kwargs: Any) -> Any:
        self.order.append("stream")
        async for item in super().stream_events(**kwargs):
            yield item


def draft_ready(event_id: str, draft_id: str) -> Any:
    return ev(event_id, "draft.ready", {"draft_id": draft_id, "level": "N3"})


async def _noop(channel: str, reply: Reply) -> None:
    return None


def test_draft_ready_is_a_subscribed_topic_but_not_a_text_notification() -> None:
    assert TOPIC_CHANNELS["draft.ready"] == "cases"
    assert format_event(draft_ready("1-1", DRAFT), "en") is None


async def test_notifier_posts_draft_ready_with_buttons_then_marks_notified() -> None:
    view = make_view()
    gate = DraftGate([[draft_ready("1-1", DRAFT)]], [], cursor="0-1")
    gate.drafts[DRAFT] = view
    sent: list[tuple[str, Reply, str]] = []

    async def send_draft(channel: str, reply: Reply, draft_id: str) -> bool:
        sent.append((channel, reply, draft_id))
        return True

    await run_until(gate, _noop, lambda: bool(gate.notified), send_draft=send_draft)
    assert gate.notified == [DRAFT]
    channel, reply, draft_id = sent[0]
    assert channel == "cases" and draft_id == DRAFT
    # notifier locale is "en"; public cards are not ephemeral
    assert "Fact-check draft" in joined(reply) and "`False`" in joined(reply)
    assert not reply.ephemeral and reply.buttons
    assert gate.stored == ["1-1"]


async def test_notifier_skips_replayed_decided_and_missing_drafts_and_survives_errors() -> None:
    done = make_view(draft_id="done", notified_at="2026-11-11T10:00:00")
    decided = make_view(draft_id="decided", status="active")
    fresh = make_view(draft_id="fresh")
    gate = DraftGate(
        [
            [
                draft_ready("1-1", "done"),
                draft_ready("1-2", "decided"),
                ev("1-3", "draft.ready", {}),
                draft_ready("1-4", "fresh"),
            ]
        ],
        [done, decided, fresh],
        cursor="0-1",
    )
    sent: list[str] = []

    async def send_draft(channel: str, reply: Reply, draft_id: str) -> bool:
        sent.append(draft_id)
        return True

    await run_until(gate, _noop, lambda: gate.stored[-1:] == ["1-4"], send_draft=send_draft)
    assert sent == ["fresh"] and gate.notified == ["fresh"]

    failing = DraftGate([[draft_ready("2-1", "fresh"), draft_ready("2-2", "fresh")]], [fresh])
    failing.fetch_error = GateUnavailable(0, "TWR-BOT-001", "down")
    await run_until(failing, _noop, lambda: failing.stored[-1:] == ["2-2"], send_draft=send_draft)
    assert failing.notified == []  # nothing marked: the next reconciliation retries


async def test_notifier_does_not_mark_a_draft_that_was_not_posted() -> None:
    gate = DraftGate([[draft_ready("1-1", DRAFT)]], [make_view()], cursor="0-1")

    async def send_draft(channel: str, reply: Reply, draft_id: str) -> bool:
        return False  # channel not configured

    await run_until(gate, _noop, lambda: gate.stored[-1:] == ["1-1"], send_draft=send_draft)
    assert gate.notified == []


async def test_notifier_reconciles_before_reading_events() -> None:
    drafts = [make_view(draft_id=f"p{n}", seq=n) for n in (1, 2)]
    gate = DraftGate([[]], drafts, cursor="0-1")
    sent: list[str] = []

    async def send_draft(channel: str, reply: Reply, draft_id: str) -> bool:
        sent.append(draft_id)
        return True

    await run_until(gate, _noop, lambda: len(gate.notified) == 2, send_draft=send_draft)
    assert gate.order[0] == "list" and "stream" in gate.order
    assert gate.order.index("list") < gate.order.index("stream")
    assert sent == ["p2", "p1"] or sorted(sent) == ["p1", "p2"]


async def test_notifier_retries_reconciliation_when_gate_is_down() -> None:
    drafts = [make_view(draft_id="p1", seq=1)]
    gate = DraftGate([[]], drafts, cursor="0-1")
    real_list = gate.list_drafts
    attempts = {"n": 0}

    async def flaky(acting: int | None, **kwargs: Any) -> list[dict[str, Any]]:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise GateUnavailable(0, "TWR-BOT-001", "down")
        return await real_list(acting, **kwargs)

    gate.list_drafts = flaky  # type: ignore[method-assign]
    sent: list[str] = []

    async def send_draft(channel: str, reply: Reply, draft_id: str) -> bool:
        sent.append(draft_id)
        return True

    await run_until(gate, _noop, lambda: bool(sent), send_draft=send_draft)
    assert attempts["n"] >= 2


async def test_notifier_without_send_draft_ignores_drafts() -> None:
    gate = DraftGate([[draft_ready("1-1", DRAFT)]], [make_view()], cursor="0-1")
    await run_until(gate, _noop, lambda: gate.stored[-1:] == ["1-1"])
    assert gate.notified == []


# --- discord wiring (no connection) --------------------------------------------------


async def test_discord_client_builds_with_draft_ui_and_factcheck_command() -> None:
    from tower.intercom import commands
    from tower.intercom.app import _build_client

    settings = Settings(process="test")
    gate = GateClient("http://gate.invalid", "token")
    try:
        client = _build_client(settings, gate, asyncio.Event(), {"cases": 1}, "fr", dry_run=True)
        names = {c.name for c in client.tree.get_commands()}
        assert {"factcheck", "group", "fact", "narrative"} <= names
        items = commands.dynamic_items(commands.Context(gate=gate, default_locale="fr"))
        patterns = [cast(Any, i).__discord_ui_compiled_template__ for i in items]
        for custom_id in (f"t12a:fc:validate:{DRAFT}", f"fc:validate:{DRAFT}"):
            assert sum(1 for p in patterns if p.fullmatch(custom_id)) == 1, custom_id
        assert not any(p.fullmatch("fc:other:x") for p in patterns)
    finally:
        await gate.aclose()


# --- cards and persistent components (pure) ------------------------------------------


def test_draft_card_controls_are_persistent_and_on_the_last_card() -> None:
    from tests.intercom_ui import assert_persistent

    reply = fc.render_draft(make_view(), "en")
    assert [b.label for b in reply.buttons] == ["Validate", "Correct", "Reject"]
    assert [b.label for b in reply.fields[0].control] == [  # type: ignore[union-attr]
        "Same group as...",
        "Split",
        "Create fact",
        "Validate band",
    ]
    assert reply.footer == f"Draft `{DRAFT[:8]}`" and not reply.ephemeral
    assert_persistent(reply)
    assert not fc.render_draft(make_view(), "en", buttons=False).buttons


def test_draft_cards_render_under_the_v2_limits() -> None:
    from discord import ui

    from tower.intercom.ui import render

    long_note = {"fr": {"text": "mot " * 600, "fact_sheet": None, "origin": "machine"}}
    reply = fc.render_draft(make_view(notes=long_note, not_in_sources=["x" * 390] * 12), "fr")
    for card in cards(reply):
        view = render.layout(card)
        total = sum(len(i.content) for i in view.walk_children() if isinstance(i, ui.TextDisplay))
        assert total <= 4000 and sum(1 for _ in view.walk_children()) <= 40


def test_settle_view_removes_the_decision_buttons() -> None:
    from discord import ui

    from tower.intercom.commands.drafts import settle_view
    from tower.intercom.ui import render

    def custom_ids(view: ui.LayoutView) -> list[str]:
        return [
            i.custom_id for i in view.walk_children() if isinstance(i, ui.Button) and i.custom_id
        ]

    reply = fc.render_draft(make_view(), "en")
    rejected = render.layout(reply)
    assert len(custom_ids(rejected)) == 7
    assert settle_view(rejected, status="rejected", locale="en")
    assert custom_ids(rejected) == []
    texts = [i.content for i in rejected.walk_children() if isinstance(i, ui.TextDisplay)]
    assert "-# Draft rejected." in texts
    assert not any("Group and reference fact" in x for x in texts)
    validated = render.layout(reply)
    settle_view(validated, status="active", locale="en")
    # the group and reference-fact buttons stay: a fact needs a validated verdict
    assert [c.split(":")[2] for c in custom_ids(validated)] == [
        "samegroup",
        "split",
        "fact",
        "band",
    ]


def test_group_fact_and_request_cards_are_persistent() -> None:
    from tests.intercom_ui import assert_persistent

    for reply in (
        fc._group_done("en", "group.merged.title", "group.merged", {"id": DRAFT, "members": [1]}),
        fc._ambiguous("en", "abcdef"),
        fc.decided_reply("en", "active"),
    ):
        assert_persistent(reply)
