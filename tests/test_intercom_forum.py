"""Forum of cases: pure rendering (post, sections, tags, activity) and the event flow
with a fake gate and a fake Discord sink."""

from __future__ import annotations

import copy
from typing import Any, cast

import pytest

from tests.intercom_ui import assert_persistent, custom_ids
from tests.test_intercom_factcheck import DRAFT, make_view
from tower.intercom.forum import CaseForum
from tower.intercom.gate_client import GateClient
from tower.intercom.handlers import MAX_MESSAGE, Reply
from tower.intercom.handlers import forum as fr
from tower.intercom.i18n import t
from tower.intercom.ui import emojis

CASE = "11111111-2222-3333-4444-555555555555"


def full_view(**overrides: Any) -> dict[str, Any]:
    source = {"url": "https://factually.example/a", "publisher": "Factually", "title": "t"}
    cite = {"label": "P1", "quote": "no evidence", "valid": True, "lang": "en", "source": source}
    view = make_view(
        case={"id": CASE, "status": "received", "discord_thread_id": None},
        group={"id": "8ab7ea0e-b743-485d-8d60-3acfcf7df3c0", "members": 1},
        narrative={
            "id": "2024338f-5f51-43a1-895f-4b5e9f5cb48b",
            "title": None,
            "state": "active",
            "groups": 1,
        },
        rationale=[
            {"assertion": "First", "citations": [cite, dict(cite, label="P5")]},
            {"assertion": "Second", "citations": [dict(cite, label="P3")]},
        ],
        checks={"note_too_long": True},
    )
    view.update(overrides)
    return view


# --- rendering ---------------------------------------------------------------------------


def test_tag_catalogue_fits_discord() -> None:
    assert len(fr.TAGS) <= 20
    for key in fr.TAGS:
        label = t("en", f"forum.tag.{key}")
        assert label != f"forum.tag.{key}" and len(label) <= 20


def test_post_of_a_pending_draft() -> None:
    post = fr.draft_post(full_view())
    assert post.name == "Unemployment doubled in a year"
    assert post.tags == ("status.to_review", "verdict.false", "level.N3", "signal.attention")
    assert not post.archived
    s = post.starter
    assert not s.ephemeral
    assert s.title.startswith(t("en", "forum.status.to_review"))
    assert "Unemployment doubled in a year" in s.body
    assert "`False`" in s.body
    assert any("The rate is stable." in sec for sec in s.sections)
    assert any("1 quality warning" in sec for sec in s.sections)
    # One action bar (decision + post link) and one menu, nowhere else.
    assert [b.label for b in s.buttons] == ["Validate", "Correct", "Reject", "Open post"]
    assert s.buttons[-1].url == "https://x.com/i/status/1850000000000000042"
    assert s.menu is not None and {o.value for o in s.menu.options} == set(fr.MORE_ACTIONS)
    assert not s.fields and not s.extra
    assert_persistent(s)
    assert f"t12a:fc:more:{DRAFT}" in custom_ids(s)


def test_post_title_is_clipped_to_the_discord_limit() -> None:
    long = "word " * 60
    post = fr.draft_post(full_view(claim={"text_original": long, "lang": "en"}))
    assert len(post.name) <= fr.NAME_MAX


@pytest.mark.parametrize(
    ("case_status", "draft_status", "status", "archived"),
    [
        ("in_review", "draft", "endorsed", False),
        ("validated", "active", "validated", False),
        ("published", "active", "published", True),
        ("rejected", "rejected", "rejected", True),
        ("received", "superseded", "superseded", True),
    ],
)
def test_post_follows_the_case(
    case_status: str, draft_status: str, status: str, archived: bool
) -> None:
    view = full_view(status=draft_status)
    post = fr.draft_post(view, case_status)
    assert post.tags[0] == f"status.{status}"
    assert post.archived is archived
    labels = [b.label for b in post.starter.buttons]
    if draft_status != "draft":
        assert "Validate" not in labels  # decisions are gone once decided
    if status == "validated":
        assert "Mark as published" in labels
        assert post.starter.menu is not None
        assert {o.value for o in post.starter.menu.options} == {
            "fact",
            "band",
            "group",
            "narrative",
        }
    if archived:
        assert "signal.attention" not in post.tags
    assert_persistent(post.starter)


def test_wave_and_deleted_post_signals_are_capped_at_five_tags() -> None:
    view = full_view(
        narrative={"id": "n", "state": "alert", "groups": 3}, flags={"post_deleted": True}
    )
    post = fr.draft_post(view)
    assert len(post.tags) == fr.MAX_TAGS
    assert "signal.attention" in post.tags and "signal.wave" in post.tags


def test_french_claim_is_tagged() -> None:
    view = full_view(claim={"text_fr": "Environ 1,2 million", "lang": "fr"})
    post = fr.draft_post(view)
    assert "Environ 1,2 million" in post.starter.body and "(FR)" in post.starter.body


def test_sections_are_ordered_and_sources_listed_once() -> None:
    cards = fr.draft_sections(full_view())
    titles = [c.title for c in cards]
    assert titles == ["Evidence", "Gaps & checks", "Quality warnings", "Context"]
    evidence = cards[0].body
    assert evidence.count("https://factually.example/a") == 1
    assert evidence.count("[S1]") == 4  # three citations + the source list
    assert "dropped quote" not in evidence  # invalid citation hidden
    assert all(not c.ephemeral and not c.buttons for c in cards)


def test_empty_sections_are_omitted_and_long_ones_split() -> None:
    view = full_view(checks={}, objection=None, review_points=[], not_in_sources=[])
    titles = [c.title for c in fr.draft_sections(view)]
    assert "Quality warnings" not in titles and "Gaps & checks" not in titles
    many = [{"assertion": "x" * 380, "citations": []} for _ in range(40)]
    cards = fr.draft_sections(full_view(rationale=many))
    evidence = [c for c in cards if c.title.startswith("Evidence")]
    assert len(evidence) > 1 and evidence[1].title == "Evidence (cont.)"
    assert all(len(c.body) <= MAX_MESSAGE for c in cards)


def test_icons_are_all_or_nothing() -> None:
    assert fr.all_or_none({"a": "<:a:1>", "b": ""}) == {"a": "", "b": ""}
    assert fr.all_or_none({"a": "<:a:1>"}) == {"a": "<:a:1>"}
    # Every forum icon exists: each element type shows all of its icons.
    post = fr.draft_post(full_view())
    assert all(b.icon for b in post.starter.buttons) and post.starter.icon == emojis.PENDING
    assert post.starter.menu is not None
    assert all(o.icon for o in post.starter.menu.options)
    assert all(c.icon for c in fr.draft_sections(full_view()))
    validated = fr.draft_post(full_view(status="active"), "validated")
    assert [b.icon for b in validated.starter.buttons] == [emojis.PUBLISH, emojis.LINK]
    for status, icon in (("endorsed", emojis.ENDORSED), ("superseded", emojis.MERGE)):
        draft_status = "superseded" if status == "superseded" else "draft"
        dossier_status = "in_review" if status == "endorsed" else "received"
        assert fr.draft_post(full_view(status=draft_status), dossier_status).starter.icon == icon
    line = fr.activity_line("published", "discord:1", url="https://x.com/a/status/1")
    assert line.body.startswith(emojis.PUBLISH)


def test_case_post_without_draft() -> None:
    post = fr.case_post(
        {"public_id": CASE, "status": "received", "title": "Manual check", "x_post_id": "42"}
    )
    assert post.name == "Manual check" and post.tags == ("status.to_review",)
    assert [b.label for b in post.starter.buttons] == ["Open post"]


def test_activity_lines() -> None:
    line = fr.review_activity(
        {
            "outcome": "endorsed",
            "decision": "edit",
            "final_veracity": "misleading",
            "reviewer": "discord:42",
        }
    )
    assert "<@42>" in line.body and "Misleading" in line.body and "awaiting an editor" in line.body
    rejected = fr.activity_line("rejected", "discord:7", reason="Out of scope")
    assert "rejected" in rejected.body and "Out of scope" in rejected.body


# --- event flow ----------------------------------------------------------------------------


class FakeSink:
    def __init__(self) -> None:
        self.posts: dict[int, fr.Post] = {}
        self.replies: dict[int, list[Reply]] = {}
        self.gone: set[int] = set()

    async def create(self, post: fr.Post) -> int:
        thread_id = 100 + len(self.posts)
        self.posts[thread_id] = post
        self.replies[thread_id] = []
        return thread_id

    async def update(self, thread_id: int, post: fr.Post) -> bool:
        if thread_id in self.gone or thread_id not in self.posts:
            return False
        self.posts[thread_id] = post
        return True

    async def reply(self, thread_id: int, reply: Reply) -> None:
        self.replies[thread_id].append(reply)


class ForumGate:
    def __init__(self, view: dict[str, Any]) -> None:
        self.view = view
        self.case: dict[str, Any] = {
            "public_id": CASE,
            "status": "received",
            "kind": "fact_check",
            "title": "Unemployment",
            "discord_thread_id": None,
            "draft_id": DRAFT,
        }
        self.notified: list[str] = []
        self.links: list[tuple[str, int]] = []

    async def link_discord_thread(self, public_id: str, thread_id: int) -> None:
        self.links.append((public_id, thread_id))
        self.case["discord_thread_id"] = str(thread_id)
        self.view["case"]["discord_thread_id"] = str(thread_id)

    async def get_draft(self, acting: object, draft_id: str) -> dict[str, Any]:
        return copy.deepcopy(self.view)

    async def get_case(self, acting: object, public_id: str) -> dict[str, Any]:
        return dict(self.case)

    async def mark_draft_notified(self, acting: object, draft_id: str) -> None:
        self.notified.append(draft_id)
        self.view["notified_at"] = "now"


def make_forum(view: dict[str, Any]) -> tuple[CaseForum, ForumGate, FakeSink]:
    gate, sink = ForumGate(view), FakeSink()
    return CaseForum(cast(GateClient, gate), sink), gate, sink


async def test_opening_then_draft_makes_one_post() -> None:
    forum, gate, sink = make_forum(full_view())
    await forum.on_case_opened(dict(gate.case))
    assert list(sink.posts) == [100] and gate.links == [(CASE, 100)]
    await forum.on_draft_ready({"draft_id": DRAFT})
    assert list(sink.posts) == [100]  # same post, upgraded to the full card
    assert sink.posts[100].starter.menu is not None
    assert sink.replies[100][0].title == "Evidence"
    assert gate.notified == [DRAFT]
    # A replayed draft.ready changes nothing.
    await forum.on_draft_ready({"draft_id": DRAFT})
    assert len(sink.replies[100]) == 4 and gate.notified == [DRAFT]
    # Opening replayed: the case already has its post.
    await forum.on_case_opened(dict(gate.case))
    assert list(sink.posts) == [100]


async def test_draft_without_opening_creates_the_post() -> None:
    forum, gate, sink = make_forum(full_view())
    assert await forum.show_draft(copy.deepcopy(gate.view))
    assert list(sink.posts) == [100] and gate.links == [(CASE, 100)]


async def test_deleted_post_is_recreated() -> None:
    forum, gate, sink = make_forum(full_view())
    await forum.on_case_opened(dict(gate.case))
    sink.gone.add(100)
    await forum.on_draft_ready({"draft_id": DRAFT})
    assert 101 in sink.posts and gate.links[-1] == (CASE, 101)


async def test_review_and_transitions_update_the_post() -> None:
    forum, gate, sink = make_forum(full_view())
    await forum.on_draft_ready({"draft_id": DRAFT})
    gate.view["status"] = "active"
    gate.view["case"]["status"] = "validated"
    gate.case["status"] = "validated"
    await forum.on_draft_reviewed(
        {
            "draft_id": DRAFT,
            "outcome": "validated",
            "decision": "accept",
            "final_veracity": "false",
            "reviewer": "discord:9",
        }
    )
    assert sink.posts[100].tags[0] == "status.validated"
    assert "<@9>" in sink.replies[100][-1].body
    replies = len(sink.replies[100])
    # The transition of the same decision adds no second line.
    await forum.on_case_transitioned({"public_id": CASE, "to": "validated"}, "discord:9")
    assert len(sink.replies[100]) == replies
    gate.case.update(status="published", published_url="https://x.com/s/status/1")
    gate.view["case"]["status"] = "published"
    await forum.on_case_transitioned({"public_id": CASE, "to": "published"}, "discord:9")
    assert sink.posts[100].archived and sink.posts[100].tags[0] == "status.published"
    assert "https://x.com/s/status/1" in sink.replies[100][-1].body


async def test_review_of_a_draft_never_shown_in_the_forum_is_ignored() -> None:
    forum, _gate, sink = make_forum(full_view())
    await forum.on_draft_reviewed({"draft_id": DRAFT, "outcome": "rejected"})
    assert sink.posts == {}


async def test_manual_case_transitions_are_logged() -> None:
    forum, gate, sink = make_forum(full_view())
    gate.case["draft_id"] = None
    await forum.on_case_opened(dict(gate.case))
    gate.case["status"] = "rejected"
    await forum.on_case_transitioned({"public_id": CASE, "to": "rejected"}, "discord:5")
    assert sink.posts[100].archived
    assert "Rejected" in sink.replies[100][-1].body


async def test_notifier_routes_case_events_to_the_forum() -> None:
    from tower.intercom.gate_client import SseEvent
    from tower.intercom.notifier import Notifier

    forum, gate, sink = make_forum(full_view())
    cursors: list[str] = []

    async def put_cursor(name: str, cursor: str) -> None:
        cursors.append(cursor)

    gate.put_cursor = put_cursor  # type: ignore[attr-defined]
    sent: list[tuple[str, Reply]] = []

    async def send(channel: str, reply: Reply) -> None:
        sent.append((channel, reply))

    notifier = Notifier(cast(GateClient, gate), send, cursor_name="c", locale="en", forum=forum)
    assert "draft.reviewed" in notifier.topics
    await notifier._handle(SseEvent("1-1", "case.opened", {"payload": dict(gate.case)}))
    await notifier._handle(SseEvent("1-2", "draft.ready", {"payload": {"draft_id": DRAFT}}))
    assert list(sink.posts) == [100] and sent == []  # nothing in the text channel
    assert cursors == ["1-1", "1-2"]
