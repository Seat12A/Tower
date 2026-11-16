"""Bot ``/badge`` handlers against a real gate (ASGI), in French and English."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

import httpx
import pytest

from tower.core.db.database import Database
from tower.core.domain.badges import AccountRef
from tower.core.domain.platforms import Platform
from tower.core.domain.security import Actor
from tower.core.usecases import badge_submissions as sub
from tower.intercom.gate_client import GateClient
from tower.intercom.handlers import badges as h

from .conftest import TokenFactory
from .intercom_ui import assert_persistent

REVIEWER = 2001
EDITOR = 2002
AddStaff = Callable[..., Coroutine[Any, Any, None]]
ANON = Actor.anonymous()


@pytest.fixture
async def gate(
    client: httpx.AsyncClient, make_token: TokenFactory, add_staff: AddStaff
) -> GateClient:
    token = await make_token("intercom")
    await add_staff(REVIEWER, ["reviewer"])
    await add_staff(EDITOR, ["editor"])
    return GateClient(base_url="http://gate", token=token, http=client)


def labels(reply: Any) -> list[str]:
    return [b.label for b in reply.buttons]


@pytest.mark.parametrize("locale", ["en", "fr"])
async def test_account_lifecycle(gate: GateClient, locale: str) -> None:
    reply = await h.badge_show(gate, REVIEWER, locale, "x", "4242")
    assert reply.accent == "neutral" and "x:4242" in reply.text
    assert_persistent(reply)
    reply = await h.badge_grant(gate, EDITOR, locale, "x", "4242", "media", "AFP", None, "ok")
    assert reply.accent == "primary" and "AFP" in reply.text and "#D946EF" in reply.text
    assert "v2" in reply.footer
    assert len(reply.buttons) == 5  # edit, revoke, block, history, keys (affiliable)
    assert_persistent(reply)
    reply = await h.badge_end(gate, EDITOR, locale, "x", "4242", "block", "fake")
    assert reply.accent == "warning" and "fake" in reply.text
    assert_persistent(reply)
    reply = await h.badge_end(gate, EDITOR, locale, "x", "4242", "unblock")
    assert reply.accent == "neutral"
    reply = await h.badge_history(gate, REVIEWER, locale, "x", "4242")
    assert len(reply.items) == 3 and "staff:2002@intercom" in reply.text
    assert_persistent(reply)


@pytest.mark.parametrize("locale", ["en", "fr"])
async def test_handle_is_refused_and_reviewer_cannot_grant(gate: GateClient, locale: str) -> None:
    reply = await h.badge_show(gate, REVIEWER, locale, "x", "@jules")
    assert reply.accent == "warning" and "TWR-ING-006" in reply.footer
    reply = await h.badge_grant(gate, REVIEWER, locale, "x", "1", "expert")
    assert "TWR-AUTH-005" in reply.footer


@pytest.mark.parametrize("locale", ["en", "fr"])
async def test_affiliation_flow(gate: GateClient, locale: str) -> None:
    await h.badge_grant(gate, EDITOR, locale, "x", "100", "organization", "Club")
    reply = await h.affiliate(gate, EDITOR, locale, "x", "200", "100", "Coach", True)
    assert reply.accent == "warning"  # pending
    assert_persistent(reply)
    short_id = reply.buttons[0].custom_id.rsplit(":", 1)[-1][:8]
    reply = await h.affiliation_act(gate, EDITOR, locale, short_id, "validate")
    assert reply.accent == "primary" and "Coach" in reply.text
    assert_persistent(reply)
    shown = await h.badge_show(gate, REVIEWER, locale, "x", "200")
    assert shown.items and "Club" in shown.items[0]
    reply = await h.affiliation_act(gate, EDITOR, locale, short_id, "revoke", "left")
    assert reply.accent == "neutral" and "left" in reply.text
    reply = await h.affiliation_act(gate, EDITOR, locale, short_id, "reconfirm")
    assert "TWR-BDG-008" in reply.footer


@pytest.mark.parametrize("locale", ["en", "fr"])
async def test_queue_requests_and_reports(gate: GateClient, db: Database, locale: str) -> None:
    async with db.transaction() as s:
        r = await sub.submit_request(
            s,
            ANON,
            kind="badge",
            account=AccountRef(Platform.X, "300"),
            category="expert",
            justification="Researcher",
            contact="me@example.org",
        )
    reply = await h.queue(gate, REVIEWER, locale, "requests")
    assert len(reply.items) == 1 and reply.fields
    assert_persistent(reply)
    reply = await h.request_show(gate, REVIEWER, locale, str(r.public_id)[:8])
    assert "me@example.org" in reply.text and "Researcher" in reply.text
    assert_persistent(reply)
    reply = await h.request_decide(gate, EDITOR, locale, str(r.public_id), True, "media", "ok")
    assert reply.accent == "neutral" and reply.footer
    shown = await h.badge_show(gate, REVIEWER, locale, "x", "300")
    assert "media" in shown.text
    async with db.transaction() as s:
        rep = await sub.submit_report(
            s, ANON, account=AccountRef(Platform.X, "300"), reason="impersonation"
        )
    reply = await h.queue(gate, REVIEWER, locale, "reports")
    assert len(reply.items) == 1
    reply = await h.report_resolve(gate, EDITOR, locale, str(rep.public_id)[:8], "dismissed")
    assert reply.accent == "neutral"
    assert_persistent(reply)
    reply = await h.queue(gate, REVIEWER, locale, "reports")
    assert not reply.items and not reply.fields


@pytest.mark.parametrize("locale", ["en", "fr"])
async def test_org_keys_and_imports(gate: GateClient, locale: str) -> None:
    await h.badge_grant(gate, EDITOR, locale, "x", "500", "institutional", "Ville")
    reply = await h.org_key_create(gate, EDITOR, locale, "x", "500", "RH")
    assert "t12o_" in reply.text and reply.ephemeral
    assert_persistent(reply)
    reply = await h.org_keys(gate, REVIEWER, locale, "x", "500")
    assert len(reply.items) == 1 and reply.fields
    assert_persistent(reply)
    key_id = reply.fields[0].control.options[0].value  # type: ignore[union-attr]
    reply = await h.org_key_revoke(gate, EDITOR, locale, "x", "500", key_id[:8])
    assert reply.footer and not reply.fields
    reply = await h.badge_start_import(gate, EDITOR, locale, "Q142 intl")
    assert reply.accent == "success" and len(reply.items) == 2
    reply = await h.badge_imports(gate, REVIEWER, locale)
    assert len(reply.items) == 2
