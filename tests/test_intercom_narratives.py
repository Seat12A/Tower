"""Bot: wave notifications (idempotent per narrative and tier), draft rendering of the
narrative and coordination signals, and /narrative commands against a real gate."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from tower.core.db.database import Database
from tower.intercom.gate_client import GateClient
from tower.intercom.handlers import MAX_MESSAGE
from tower.intercom.handlers import factcheck as fc
from tower.intercom.handlers import narratives as nh
from tower.intercom.notifier import TOPIC_CHANNELS, format_event

from .conftest import TokenFactory
from .test_factcheck_matching import _reset  # noqa: F401  (autouse fixture)
from .test_factcheck_narratives import build_narrative, narrative_public, public_id, seed
from .test_factcheck_narratives import unit as axis
from .test_intercom_factcheck import make_view
from .test_intercom_notifier import FakeGate, ev, run_until

REVIEWER = 1001
EDITOR = 1002
LOCALES = ["en", "fr"]
NID = "aaaaaaaa-1111-2222-3333-444444444444"
UNKNOWN = "11111111-2222-3333-4444-555555555555"
AddStaff = Callable[..., Coroutine[Any, Any, None]]

SURGE = {
    "narrative_id": NID,
    "tier": "surge",
    "share": 0.034,
    "accounts": 41,
    "platforms": ["bluesky", "x"],
    "title": "Fausse hausse des prix",
    "representative_group_id": None,
}
ENDED = {
    "narrative_id": NID,
    "duration_s": 7500,
    "posts": 1234,
    "platforms": ["x"],
    "cost_usd_micros": 2_500_000,
    "first_draft_delay_s": 1500,
}


# --- notifications ----------------------------------------------------------------------


def test_wave_topics_go_to_the_dossiers_channel() -> None:
    assert TOPIC_CHANNELS["narrative.surge_detected"] == "dossiers"
    assert TOPIC_CHANNELS["narrative.surge_ended"] == "dossiers"


@pytest.mark.parametrize("locale", LOCALES)
def test_surge_alert_and_report_texts(locale: str) -> None:
    surge = format_event(ev("1-1", "narrative.surge_detected", SURGE), locale)
    assert surge is not None and surge[0] == "dossiers"
    for needle in ("aaaaaaaa", "Fausse hausse des prix", "3.4%", "41", "bluesky, x", NID):
        assert needle in surge[1]
    alert = format_event(ev("1-2", "narrative.surge_detected", {**SURGE, "tier": "alert"}), locale)
    assert alert is not None and alert[1] != surge[1]
    assert ("ALERT" in alert[1]) if locale == "en" else ("ALERTE" in alert[1])
    ended = format_event(ev("1-3", "narrative.surge_ended", ENDED), locale)
    assert ended is not None and ended[0] == "dossiers"
    for needle in ("2h05", "1234", "2.50", "25 min"):
        assert needle in ended[1]


def test_untitled_narrative_and_missing_first_draft_are_rendered() -> None:
    text_ = nh.format_surge_detected({**SURGE, "title": None, "platforms": None}, "en")
    assert "(untitled)" in text_ and "platforms: -" in text_
    assert "first draft: -" in nh.format_surge_ended({**ENDED, "first_draft_delay_s": None}, "en")


async def test_a_replayed_surge_or_alert_is_notified_once_per_tier() -> None:
    alert = {**SURGE, "tier": "alert"}
    gate = FakeGate(
        [
            [
                ev("1-1", "narrative.surge_detected", SURGE),
                ev("1-2", "narrative.surge_detected", SURGE),  # another event, same tier
                ev("1-3", "narrative.surge_detected", alert),
                ev("1-1", "narrative.surge_detected", SURGE),  # same event id replayed
                ev("1-4", "narrative.surge_detected", alert),
                ev("1-5", "narrative.surge_ended", ENDED),
                ev("1-6", "narrative.surge_detected", SURGE),  # a later wave of the narrative
            ]
        ]
    )
    sent: list[tuple[str, str]] = []

    async def send(channel: str, body: str) -> None:
        sent.append((channel, body))

    await run_until(gate, send, lambda: len(gate.stored) >= 6)
    kinds = [("alert" if "ALERT" in b else "ended" if "over" in b else "surge") for _, b in sent]
    assert kinds == ["surge", "alert", "ended", "surge"]
    assert {c for c, _ in sent} == {"dossiers"}


async def test_two_narratives_are_notified_separately() -> None:
    other = {**SURGE, "narrative_id": "bbbbbbbb-1111-2222-3333-444444444444"}
    gate = FakeGate(
        [
            [
                ev("1-1", "narrative.surge_detected", SURGE),
                ev("1-2", "narrative.surge_detected", other),
            ]
        ]
    )
    sent: list[tuple[str, str]] = []

    async def send(channel: str, body: str) -> None:
        sent.append((channel, body))

    await run_until(gate, send, lambda: len(gate.stored) >= 2)
    assert len(sent) == 2


# --- draft rendering --------------------------------------------------------------------


def wave_view() -> dict[str, Any]:
    view = make_view()
    view.update(
        narrative={"id": NID, "title": "Fausse hausse des prix", "state": "surge", "groups": 4},
        coordination=[
            {"kind": "near_identical_burst", "count": 12, "window_minutes": 10, "detail": {}},
            {
                "kind": "new_accounts",
                "count": 6,
                "window_minutes": 10080,
                "detail": {"authors": 10},
            },
            {"kind": "installation_burst", "count": 25, "window_minutes": 10, "detail": {}},
            {"kind": "media_reuse", "count": 2, "window_minutes": 0, "detail": {}},
            {"kind": "future_kind", "count": 3, "window_minutes": 0, "detail": {}},
        ],
    )
    return view


@pytest.mark.parametrize("locale", LOCALES)
def test_render_shows_the_narrative_and_the_coordination_block(locale: str) -> None:
    parts = fc.render_draft(wave_view(), locale)
    body = "\n".join(parts)
    assert all(len(p) <= MAX_MESSAGE for p in parts)
    assert f"`{NID}`" in body and "Fausse hausse des prix" in body
    assert "12 " in body and "25 " in body and "6 " in body and "future_kind" in body
    if locale == "en":
        assert "informational, never a verdict input" in body and "surge" in body
        assert "first seen by Tower" in body
    else:
        assert "informatifs, jamais une entrée du verdict" in body and "vague" in body
        assert "première observation" in body


@pytest.mark.parametrize("locale", LOCALES)
def test_render_without_narrative_or_signals_is_unchanged(locale: str) -> None:
    body = "\n".join(fc.render_draft(make_view(), locale))
    for needle in ("Narrative", "Narratif", "oordination"):
        assert needle not in body


# --- commands against the gate ------------------------------------------------------------


@pytest.fixture
async def gate(
    client: httpx.AsyncClient, make_token: TokenFactory, add_staff: AddStaff
) -> GateClient:
    token = await make_token("intercom")
    await add_staff(REVIEWER, ["reviewer"])
    await add_staff(EDITOR, ["editor"])
    return GateClient(base_url="http://gate", token=token, http=client)


async def make_narrative(db: Database, base: int, groups: int = 3) -> tuple[int, list[int]]:
    gids = [await seed(db, f"b{base}-{i}", axis(base + i), reach=i + 1) for i in range(groups)]
    return await build_narrative(db, *gids), gids


async def test_discord_client_registers_the_narrative_commands(gate: GateClient) -> None:
    from tower.intercom.app import _build_client

    from .conftest import make_settings

    client = _build_client(make_settings(), gate, asyncio.Event(), {"dossiers": 1}, "en", True)
    commands = {
        c.name: {s.name for s in getattr(c, "commands", [])} for c in client.tree.get_commands()
    }
    assert commands["narrative"] == {"list", "show", "merge", "split", "title"}


@pytest.mark.parametrize("locale", LOCALES)
async def test_list_and_show(gate: GateClient, db: Database, locale: str) -> None:
    empty = await nh.narrative_list(gate, REVIEWER, locale)
    assert (empty.ephemeral and "No narrative" in empty.text) or "Aucun narratif" in empty.text
    nid, gids = await make_narrative(db, 100)
    async with db.transaction() as s:
        await s.execute(
            text(
                "UPDATE narratives SET title = 'Story', title_status = 'draft', "
                "state = 'watch', split_suggested = true WHERE id = :n"
            ),
            {"n": nid},
        )
        await s.execute(
            text(
                "INSERT INTO narrative_stats (narrative_id, span, window_end, posts, flow_posts, "
                "share, accounts, platforms, reach, score) "
                "VALUES (:n, '1h', now(), 5, 100, 0.05, 33, 2, 0, 1)"
            ),
            {"n": nid},
        )
    public = str(await narrative_public(db, nid))
    listing = await nh.narrative_list(gate, REVIEWER, locale)
    assert public[:8] in listing.text and "Story" in listing.text and "5.0%" in listing.text
    filtered = await nh.narrative_list(gate, REVIEWER, locale, "surge")
    assert public[:8] not in filtered.text
    shown = await nh.narrative_show(gate, REVIEWER, locale, public)
    assert shown.ephemeral and len(shown.text) <= MAX_MESSAGE
    assert public in shown.text and "Story" in shown.text and "33" in shown.text
    assert str(await public_id(db, gids[2])) in shown.text
    assert ("split is suggested" if locale == "en" else "Scission suggérée") in shown.text


@pytest.mark.parametrize("locale", LOCALES)
async def test_show_errors_are_translated_with_their_code(gate: GateClient, locale: str) -> None:
    missing = await nh.narrative_show(gate, REVIEWER, locale, UNKNOWN)
    assert "TWR-FCK-043" in missing.text and missing.ephemeral
    bad = await nh.narrative_show(gate, REVIEWER, locale, "abc")
    assert "TWR-GEN-001" in bad.text


@pytest.mark.parametrize("locale", LOCALES)
async def test_merge_split_and_title(gate: GateClient, db: Database, locale: str) -> None:
    a, _ = await make_narrative(db, 120)
    b, _ = await make_narrative(db, 140, groups=1)
    target, source = str(await narrative_public(db, a)), str(await narrative_public(db, b))
    denied = await nh.narrative_merge(gate, REVIEWER, locale, target, source, "same")
    assert "403" in denied.text or "TWR-" in denied.text
    merged = await nh.narrative_merge(gate, EDITOR, locale, target, source, "same story")
    assert target in merged.text and "4" in merged.text
    again = await nh.narrative_merge(gate, EDITOR, locale, target, source, "again")
    assert "TWR-FCK-044" in again.text

    detail = await gate.get_narrative(EDITOR, target)
    moved = [g["id"] for g in detail["groups"]][:2]  # by reach: the founder is last
    bad = await nh.narrative_split(gate, EDITOR, locale, target, "", "why")
    assert "TWR-GEN-001" in bad.text
    split = await nh.narrative_split(gate, EDITOR, locale, target, ", ".join(moved), "other story")
    created = [r for r in await gate.list_narratives(EDITOR) if r["id"] != target]
    assert len(created) == 1 and created[0]["id"] in split.text
    invalid = await nh.narrative_split(gate, EDITOR, locale, target, UNKNOWN, "x")
    assert "TWR-FCK-044" in invalid.text

    async with db.transaction() as s:
        await s.execute(
            text(
                "UPDATE narratives SET title = 'Draft', title_status = 'draft' WHERE public_id = :p"
            ),
            {"p": target},
        )
    done = await nh.narrative_title(gate, REVIEWER, locale, target)
    assert "Draft" in done.text
    edited = await nh.narrative_title(gate, REVIEWER, locale, target, "  Corrected  ")
    assert "Corrected" in edited.text
    detail = await gate.get_narrative(REVIEWER, target)
    assert detail["title"] == "Corrected" and detail["title_status"] == "validated"
    unknown = await nh.narrative_title(gate, REVIEWER, locale, UNKNOWN)
    assert "TWR-FCK-043" in unknown.text


async def test_wave_alert_mentions_the_configured_role_only_at_tier_alert() -> None:
    alert = {**SURGE, "tier": "alert"}
    gate = FakeGate(
        [
            [
                ev("1-1", "narrative.surge_detected", SURGE),
                ev("1-2", "narrative.surge_detected", alert),
            ]
        ]
    )
    sent: list[str] = []

    async def send(channel: str, body: str) -> None:
        sent.append(body)

    await run_until(gate, send, lambda: len(gate.stored) >= 2, alert_role_id=123456789)
    assert not sent[0].startswith("<@&")
    assert sent[1].startswith("<@&123456789> ") and "ALERT" in sent[1]
