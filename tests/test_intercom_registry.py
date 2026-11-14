"""Bot ``/registry`` handlers against a real gate (ASGI), in French and English."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

import httpx
import pytest

from tower.core.db.database import Database
from tower.core.domain.security import Actor, Permission, Role
from tower.core.usecases import registry as uc
from tower.intercom.gate_client import GateClient
from tower.intercom.handlers import registry as h

from .conftest import TokenFactory

REVIEWER = 1001
EDITOR = 1002
AddStaff = Callable[..., Coroutine[Any, Any, None]]
SERVICE = Actor.service("intercom", frozenset(Permission))
EDITOR_ACTOR = Actor.staff(EDITOR, frozenset({Role.EDITOR}), SERVICE)


@pytest.fixture
async def gate(
    client: httpx.AsyncClient, make_token: TokenFactory, add_staff: AddStaff, db: Database
) -> GateClient:
    token = await make_token("intercom")
    await add_staff(REVIEWER, ["reviewer"])
    await add_staff(EDITOR, ["editor"])
    async with db.transaction() as s:
        await uc.create_entry(
            s,
            EDITOR_ACTOR,
            domain="example.org",
            publisher="Example",
            tier="secondary",
            kind="press",
            country="FR",
            reason="Public correction policy",
        )
    return GateClient(base_url="http://gate", token=token, http=client)


@pytest.mark.parametrize("locale", ["en", "fr"])
async def test_show_and_history(gate: GateClient, locale: str) -> None:
    reply = await h.registry_show(gate, REVIEWER, locale, "https://www.example.org/")
    assert "example.org" in reply.text and "secondary" in reply.text and reply.ephemeral
    reply = await h.registry_history(gate, REVIEWER, locale, "example.org")
    assert "v1" in reply.text and "created" in reply.text


@pytest.mark.parametrize("locale", ["en", "fr"])
async def test_unknown_domain_shows_code(gate: GateClient, locale: str) -> None:
    reply = await h.registry_show(gate, REVIEWER, locale, "nope.org")
    assert "TWR-ATL-012" in reply.text


async def test_set_tier_and_control(gate: GateClient) -> None:
    reply = await h.registry_set_tier(gate, EDITOR, "en", "example.org", "PRIMARY", "Official")
    assert "primary" in reply.text and "v2" in reply.text
    reply = await h.registry_set_control(gate, EDITOR, "en", "example.org", "Some Group", "Report")
    assert "Some Group" in reply.text and "v3" in reply.text
    shown = await h.registry_show(gate, EDITOR, "en", "example.org")
    assert "Some Group" in shown.text and "team" in shown.text
    reply = await h.registry_set_control(gate, EDITOR, "fr", "example.org", "none", "Erreur")
    assert "effacé" in reply.text and "v4" in reply.text


async def test_write_needs_a_reason_and_the_right_role(gate: GateClient) -> None:
    reply = await h.registry_set_tier(gate, EDITOR, "en", "example.org", "primary", "  ")
    assert "TWR-ATL-013" in reply.text
    reply = await h.registry_set_tier(gate, EDITOR, "en", "example.org", "gold", "x")
    assert "TWR-ATL-013" in reply.text
    reply = await h.registry_set_tier(gate, REVIEWER, "en", "example.org", "primary", "x")
    assert "TWR-AUTH-005" in reply.text
    reply = await h.registry_seed_control(gate, REVIEWER, "en", "example.org")
    assert "TWR-AUTH-005" in reply.text


async def test_seed_control_queues_once(gate: GateClient) -> None:
    first = await h.registry_seed_control(gate, EDITOR, "en", "example.org")
    second = await h.registry_seed_control(gate, EDITOR, "en", "example.org")
    assert "queued" in first.text and "already" in second.text
