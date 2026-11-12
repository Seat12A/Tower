"""Bot: draft rendering of groups, facts and bands; buttons, modals and commands against
a real gate (ASGI), in French and English."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Coroutine
from typing import Any

import httpx
import pytest

from tower.core.db.database import Database
from tower.core.models.factcheck import ClaimGroup
from tower.intercom.gate_client import GateClient
from tower.intercom.handlers import MAX_MESSAGE
from tower.intercom.handlers import factcheck as fc

from .conftest import TokenFactory
from .test_drafts_usecases import Built, build_draft
from .test_factcheck_references import active_draft, new_fact, prepare_group
from .test_gate_groups_references import add_post, claim_public, group_public
from .test_intercom_factcheck import DRAFT, make_view

REVIEWER = 1001
EDITOR = 1002
LOCALES = ["en", "fr"]
UNKNOWN = "11111111-2222-3333-4444-555555555555"
AddStaff = Callable[..., Coroutine[Any, Any, None]]


@pytest.fixture
async def gate(
    client: httpx.AsyncClient, make_token: TokenFactory, add_staff: AddStaff
) -> GateClient:
    token = await make_token("intercom")
    await add_staff(REVIEWER, ["reviewer"])
    await add_staff(EDITOR, ["editor"])
    return GateClient(base_url="http://gate", token=token, http=client)


def rich_view() -> dict[str, Any]:
    view = make_view()
    view.update(
        group={"id": "g-1234", "level": "N3", "members": 3},
        platforms=["bluesky", "x"],
        other_posts=[
            {
                "platform": "x",
                "external_id": "2",
                "url": "https://x.com/i/status/2",
                "posted_at": None,
            },
            {"platform": "bluesky", "external_id": "b", "url": None, "posted_at": None},
        ],
        reusable_verdict={"id": "v-1", "veracity": "mostly_true", "group_id": "g-9999"},
        reference_fact={
            "indicator": "fr.unemployment_rate",
            "label": "Taux de chômage",
            "value": "7.3",
            "unit": "percent",
            "period_start": "2026-01-01",
            "period_end": "2026-03-31",
            "territory": "FR",
            "status": "validated",
        },
        band={"band": "within_1_tau", "side": "above", "label": "x"},
        outdated=True,
        external_factchecks=[
            {"url": "https://fc.example/a", "publisher": "Ex", "rating": "False", "title": "t"}
        ],
    )
    return view


# --- rendering ----------------------------------------------------------------------


@pytest.mark.parametrize("locale", LOCALES)
def test_render_shows_group_fact_band_and_external_links(locale: str) -> None:
    parts = fc.render_draft(rich_view(), locale)
    text = "\n".join(parts)
    assert all(len(p) <= MAX_MESSAGE for p in parts)
    assert "`g-1234`" in text and "3" in text and "bluesky, x" in text
    assert "<https://x.com/i/status/2>" in text
    assert "`g-9999`" in text  # the related group suggestion
    assert "Taux de chômage" in text and "7.3" in text and "2026-03-31" in text
    assert "<https://fc.example/a>" in text and "False" in text
    if locale == "en":
        assert "within 1 tau of the fact (above)" in text and "outdated" in text
        assert "Mostly true" in text
    else:
        assert "à moins de 1 tau du fait (above)" in text and "périmée" in text
        assert "Plutôt vrai" in text


@pytest.mark.parametrize("locale", LOCALES)
def test_render_without_the_new_fields_is_unchanged(locale: str) -> None:
    text = "\n".join(fc.render_draft(make_view(), locale))
    for needle in ("g-1234", "Reference", "Référence", "Band", "Bande", "Fact-checks"):
        assert needle not in text


def test_custom_ids_of_the_new_buttons() -> None:
    for action in ("samegroup", "split", "fact", "band"):
        assert action in fc.ACTIONS
        assert fc.parse_custom_id(fc.custom_id(action, DRAFT)) == (action, DRAFT)
        assert len(fc.custom_id(action, DRAFT)) <= 100


async def test_discord_client_registers_the_commands_and_buttons(gate: GateClient) -> None:
    from tower.intercom.app import _build_client

    from .conftest import make_settings

    client = _build_client(make_settings(), gate, asyncio.Event(), {"dossiers": 1}, "en", True)
    commands = {
        c.name: {s.name for s in getattr(c, "commands", [])} for c in client.tree.get_commands()
    }
    assert commands["group"] == {"show", "merge", "split"}
    assert commands["fact"] == {"show", "list", "validate", "reject", "band"}
    template = client.draft_item.__discord_ui_compiled_template__
    for action in fc.ACTIONS:
        button = client.draft_item(action, DRAFT, "en")
        assert template.fullmatch(button.item.custom_id)
        assert button.item.label


# --- buttons and commands against the gate -----------------------------------------


async def two_groups(db: Database) -> tuple[Built, Built]:
    return (
        await build_draft(db, ext="1850000000000000301"),
        await build_draft(db, ext="1850000000000000302"),
    )


@pytest.mark.parametrize("locale", LOCALES)
async def test_same_group_button_merges_the_given_group_into_the_draft_group(
    gate: GateClient, db: Database, locale: str
) -> None:
    a, b = await two_groups(db)
    target, source = await group_public(db, a), await group_public(db, b)
    reply = await fc.group_merge_same(gate, EDITOR, locale, str(a.verdict), source, "same story")
    assert target in reply.text and "2" in reply.text and reply.ephemeral
    detail = await gate.get_group(EDITOR, source)
    assert detail["state"] == "merged" and detail["merged_into"] == target
    reply = await fc.group_merge_same(gate, EDITOR, locale, str(a.verdict), source, None)
    assert "TWR-FCK-021" in reply.text
    reply = await fc.group_merge_same(gate, EDITOR, locale, str(a.verdict), "not-an-id", None)
    assert "TWR-GEN-001" in reply.text
    reply = await fc.group_merge_same(gate, REVIEWER, locale, str(a.verdict), target, None)
    assert "TWR-AUTH-005" in reply.text


@pytest.mark.parametrize("locale", LOCALES)
async def test_same_group_direction_error_is_shown(
    gate: GateClient, db: Database, locale: str
) -> None:
    plain = await build_draft(db, ext="1850000000000000301")
    holder = await build_draft(db, ext="1850000000000000302", previous_active=True)
    reply = await fc.group_merge_same(
        gate, EDITOR, locale, str(plain.verdict), await group_public(db, holder), None
    )
    assert "TWR-FCK-023" in reply.text


@pytest.mark.parametrize("locale", LOCALES)
async def test_split_button_and_command(gate: GateClient, db: Database, locale: str) -> None:
    built = await build_draft(db, ext="1850000000000000301")
    one, _ = await add_post(db, built.group_id, "bluesky", "bsky-1")
    two, _ = await add_post(db, built.group_id, "bluesky", "bsky-2")
    claims = f"{await claim_public(db, one)}, {await claim_public(db, two)}"
    reply = await fc.group_split_draft(gate, EDITOR, locale, str(built.verdict), claims, "other")
    # The reply names the new group (a random UUID: never test for a given digit).
    assert re.search(r"`[0-9a-f-]{36}`", reply.text) and "TWR" not in reply.text
    assert len((await gate.get_group(EDITOR, await group_public(db, built)))["members"]) == 1

    group = await group_public(db, built)
    bad = await fc.group_split(gate, EDITOR, locale, group, claims, "again")
    assert "TWR-FCK-024" in bad.text  # no longer members
    empty = await fc.group_split(gate, EDITOR, locale, group, "  ", "x")
    assert "TWR-GEN-001" in empty.text
    notuuid = await fc.group_split(gate, EDITOR, locale, group, "abc", "x")
    assert "TWR-GEN-001" in notuuid.text
    denied = await fc.group_split(gate, REVIEWER, locale, group, claims, "x")
    assert "TWR-AUTH-005" in denied.text


@pytest.mark.parametrize("locale", LOCALES)
async def test_group_merge_and_show_commands(gate: GateClient, db: Database, locale: str) -> None:
    a, b = await two_groups(db)
    target, source = await group_public(db, a), await group_public(db, b)
    reply = await fc.group_merge(gate, EDITOR, locale, target, source, "same")
    assert target in reply.text
    shown = await fc.group_show(gate, REVIEWER, locale, target)
    assert target in shown.text and "N3" in shown.text and "merged" in shown.text
    assert "https://x.com/i/status/1850000000000000301" in shown.text
    assert "https://x.com/i/status/1850000000000000302" in shown.text
    merged = await fc.group_show(gate, REVIEWER, locale, source)
    assert target in merged.text
    missing = await fc.group_show(gate, REVIEWER, locale, UNKNOWN)
    assert "TWR-FCK-020" in missing.text
    assert "TWR-GEN-001" in (await fc.group_show(gate, REVIEWER, locale, "abc")).text


@pytest.mark.parametrize("locale", LOCALES)
async def test_fact_button_needs_an_active_verdict(
    gate: GateClient, db: Database, locale: str
) -> None:
    waiting = await build_draft(db, ext="1850000000000000301")
    reply = await fc.fact_create(
        gate,
        EDITOR,
        locale,
        str(waiting.verdict),
        "7,3",
        "2026-01-01 2026-03-31",
        "https://www.insee.fr/x",
        "INSEE",
        "2026-05-15",
    )
    assert "TWR" not in reply.text and (await gate.list_facts(EDITOR)) == []

    built = await active_draft(db, "1850000000000000302")
    reply = await fc.fact_create(
        gate,
        EDITOR,
        locale,
        str(built.verdict),
        "7,3",
        "2026-01-01/2026-03-31",
        "https://www.insee.fr/x",
        "INSEE",
        "2026-05-15",
    )
    assert "TWR" not in reply.text
    (fact,) = await gate.list_facts(EDITOR)
    assert fact["value"] == "7.3" and fact["status"] == "pending"
    assert fact["period_start"] == "2026-01-01" and fact["period_end"] == "2026-03-31"
    bad = await fc.fact_create(
        gate,
        EDITOR,
        locale,
        str(built.verdict),
        "7",
        "tomorrow",
        "https://x.example",
        "S",
        "2026-05-15",
    )
    assert "TWR-GEN-001" in bad.text
    bad = await fc.fact_create(
        gate, EDITOR, locale, str(built.verdict), "7", "2026-01-01", "https://x.example", "S", "May"
    )
    assert "TWR-GEN-001" in bad.text
    denied = await fc.fact_create(
        gate,
        REVIEWER,
        locale,
        str(built.verdict),
        "7",
        "2026-01-01",
        "https://x.example",
        "S",
        "2026-05-15",
    )
    assert "TWR" not in denied.text  # reviewers may write facts (idempotent here)


@pytest.mark.parametrize("locale", LOCALES)
async def test_fact_commands_and_band_button(gate: GateClient, db: Database, locale: str) -> None:
    built = await active_draft(db, "1850000000000000302")
    await fc.fact_create(
        gate,
        EDITOR,
        locale,
        str(built.verdict),
        "7.3",
        "2026-01-01 2026-03-31",
        "https://www.insee.fr/x",
        "INSEE",
        "2026-05-15",
    )
    (fact,) = await gate.list_facts(EDITOR)
    prefix = fact["id"][:8]
    listed = await fc.fact_list(gate, REVIEWER, locale, "pending")
    assert prefix in listed.text and "7.3" in listed.text
    assert prefix not in (await fc.fact_list(gate, REVIEWER, locale, "validated")).text
    shown = await fc.fact_show(gate, REVIEWER, locale, prefix)
    assert "INSEE" in shown.text and "https://www.insee.fr/x" in shown.text

    # The band cannot be validated before the fact.
    early = await fc.band_validate(gate, EDITOR, locale, str(built.verdict))
    assert "TWR-FCK-028" in early.text
    assert prefix in (await fc.fact_validate(gate, EDITOR, locale, prefix)).text
    done = await fc.band_validate(gate, EDITOR, locale, str(built.verdict))
    assert "TWR" not in done.text and done.ephemeral
    again = await fc.band_validate(gate, REVIEWER, locale, str(built.verdict))
    assert "TWR-AUTH-005" in again.text

    assert "TWR-FCK-025" in (await fc.fact_show(gate, EDITOR, locale, "abcdef12")).text
    assert "TWR-GEN-001" in (await fc.fact_show(gate, EDITOR, locale, "ab")).text
    assert "TWR-GEN-001" in (await fc.band_validate(gate, EDITOR, locale, "nope")).text


@pytest.mark.parametrize("locale", LOCALES)
async def test_fact_reject_and_permissions(gate: GateClient, db: Database, locale: str) -> None:
    fact = await new_fact(db, validate=False)
    prefix = str(fact.public_id)[:8]
    denied = await fc.fact_reject(gate, REVIEWER, locale, prefix, "wrong")
    assert "TWR-AUTH-005" in denied.text
    denied = await fc.fact_validate(gate, REVIEWER, locale, prefix)
    assert "TWR-AUTH-005" in denied.text
    done = await fc.fact_reject(gate, EDITOR, locale, str(fact.public_id), "wrong series")
    assert prefix in done.text
    assert "TWR-FCK-026" in (await fc.fact_reject(gate, EDITOR, locale, prefix, "again")).text
    empty = await fc.fact_list(gate, EDITOR, locale, "pending")
    assert "TWR" not in empty.text


async def test_the_gate_serves_what_the_bot_renders(gate: GateClient, db: Database) -> None:
    """End to end: a real draft view of a joined group renders with its group block."""
    fact = await new_fact(db)
    built = await build_draft(db, ext="1850000000000000301")
    await prepare_group(db, built, fact=fact)
    await add_post(db, built.group_id, "bluesky", "bsky-1")
    async with db.transaction() as s:
        group = await s.get(ClaimGroup, built.group_id)
        assert group is not None
        group.band, group.band_side = "beyond", "above"
    view = await gate.get_draft(EDITOR, str(built.verdict))
    text = "\n".join(fc.render_draft(view, "en"))
    assert "2 claim(s)" in text and "bluesky, x" in text
    assert "beyond 3 tau of the fact (above)" in text and "7.3" in text
    assert "Taux de chômage" in text
