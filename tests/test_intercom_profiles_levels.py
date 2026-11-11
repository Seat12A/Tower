from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

import httpx
import pytest

from tower.core.db.database import Database
from tower.core.factcheck.profiles import invalidate_profile_cache
from tower.intercom.gate_client import GateClient
from tower.intercom.handlers import profiles as ph

from .conftest import TokenFactory
from .test_drafts_usecases import build_draft

REVIEWER = 1001
EDITOR = 1002
ADMIN = 1003
AddStaff = Callable[..., Coroutine[Any, Any, None]]
LOCALES = ["en", "fr"]


@pytest.fixture
async def gate(
    client: httpx.AsyncClient, make_token: TokenFactory, add_staff: AddStaff
) -> GateClient:
    invalidate_profile_cache()
    token = await make_token("intercom")
    await add_staff(REVIEWER, ["reviewer"])
    await add_staff(EDITOR, ["editor"])
    await add_staff(ADMIN, ["admin"])
    return GateClient(base_url="http://gate", token=token, http=client)


def test_parse_value() -> None:
    assert ph.parse_value("0.2") == 0.2
    assert ph.parse_value("40") == 40
    assert ph.parse_value("true") is True
    assert ph.parse_value("abc") == "abc"


@pytest.mark.parametrize("locale", LOCALES)
async def test_profile_show_set_history_rollback(gate: GateClient, locale: str) -> None:
    reply = await ph.show(gate, REVIEWER, locale)
    assert reply.ephemeral and "N4" in reply.text and "0" in reply.text.splitlines()[0]
    assert len(reply.text.splitlines()) == 7  # header, general, five levels

    reply = await ph.set_value(gate, ADMIN, locale, "draft_daily_quota", "40", "more drafts")
    assert "1" in reply.text and "draft_daily_quota" in reply.text and "40" in reply.text
    shown = await ph.show(gate, EDITOR, locale)
    assert "40" in shown.text.splitlines()[1]

    history = await ph.history(gate, REVIEWER, locale)
    assert "more drafts" in history.text and "v1" in history.text and "v0" in history.text

    reply = await ph.set_value(gate, ADMIN, locale, "levels.N1.e1_threshold", "0.2", "stricter")
    assert "levels.N1.e1_threshold" in reply.text
    reply = await ph.rollback(gate, ADMIN, locale, "oops")
    assert "1" in reply.text
    invalidate_profile_cache()


@pytest.mark.parametrize("locale", LOCALES)
async def test_profile_errors_are_translated_with_their_code(gate: GateClient, locale: str) -> None:
    reply = await ph.set_value(gate, ADMIN, locale, "draft_daily_quota", "9999", "x")
    assert "TWR-FCK-030" in reply.text and reply.ephemeral
    reply = await ph.set_value(gate, ADMIN, locale, "no.such.path", "1", "x")
    assert "TWR-FCK-030" in reply.text
    for staff in (REVIEWER, EDITOR):
        reply = await ph.set_value(gate, staff, locale, "draft_daily_quota", "40", "x")
        assert "TWR-AUTH-005" in reply.text
        reply = await ph.rollback(gate, staff, locale, "x")
        assert "TWR-AUTH-005" in reply.text


@pytest.mark.parametrize("locale", LOCALES)
async def test_level_set_list_revoke(gate: GateClient, db: Database, locale: str) -> None:
    await build_draft(db)
    reply = await ph.level_set(
        gate, EDITOR, locale, "content", "x:1850000000000000042", "N2", 6.0, "worth a look"
    )
    assert "N2" in reply.text and "1850000000000000042" in reply.text
    listing = await ph.level_list(gate, REVIEWER, locale)
    assert "worth a look" in listing.text and "N2" in listing.text
    short = listing.text.split("`")[1]
    assert len(short) == 8

    reply = await ph.level_revoke(gate, EDITOR, locale, short, "enough")
    assert short in reply.text
    empty = await ph.level_list(gate, REVIEWER, locale)
    assert "worth a look" not in empty.text
    reply = await ph.level_revoke(gate, EDITOR, locale, short, "again")
    assert "TWR-FCK-032" in reply.text  # no longer among the active overrides


@pytest.mark.parametrize("locale", LOCALES)
async def test_level_errors(gate: GateClient, locale: str) -> None:
    reply = await ph.level_set(gate, EDITOR, locale, "group", "nope", "N2", 1, "r")
    assert "TWR-FCK-031" in reply.text
    reply = await ph.level_set(gate, EDITOR, locale, "content", "x:999", "N2", 1, "r")
    assert "TWR-FCK-032" in reply.text
    reply = await ph.level_set(gate, REVIEWER, locale, "content", "x:999", "N2", 1, "r")
    assert "TWR-AUTH-005" in reply.text
    reply = await ph.level_revoke(gate, EDITOR, locale, "abc", "r")
    assert "TWR-GEN-001" in reply.text


async def test_the_commands_are_registered_on_the_tree(gate: GateClient) -> None:
    import asyncio

    from tower.intercom.app import _build_client

    from .conftest import make_settings

    client = _build_client(make_settings(), gate, asyncio.Event(), {}, "en", True)
    names = {
        c.name: {s.name for s in getattr(c, "commands", [])} for c in client.tree.get_commands()
    }
    assert names["profile"] == {"show", "history", "set", "rollback"}
    assert names["level"] == {"set", "list", "revoke"}
