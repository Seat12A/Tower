"""Team area of the bot: hierarchy, panels, rank/unrank, help (fake gate, no database)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tower.core.domain.security import GRANTABLE_PERMISSIONS
from tower.intercom.gate_client import GateClient
from tower.intercom.handlers import Reply, Select
from tower.intercom.handlers import team as h
from tower.intercom.i18n import CATALOGS, has

from .intercom_ui import assert_persistent

ME = 100
TARGET = 200
ADMIN_ID = 300


def member(
    discord_id: int, perms: list[str], *, admin: bool = False, name: str | None = None
) -> dict[str, Any]:
    return {
        "discord_id": str(discord_id),
        "display_name": name or f"M{discord_id}",
        "active": True,
        "is_admin": admin,
        "roles": [],
        "permissions": perms,
        "effective_permissions": sorted(GRANTABLE_PERMISSIONS_STR) if admin else perms,
    }


GRANTABLE_PERMISSIONS_STR = {p.value for p in GRANTABLE_PERMISSIONS}


class FakeGate:
    """A tiny in-memory gate behind ``httpx.MockTransport`` (real ``GateClient``)."""

    def __init__(self, me_perms: list[str], *, me_admin: bool = False) -> None:
        self.members: dict[int, dict[str, Any]] = {
            ME: member(ME, me_perms, admin=me_admin),
            TARGET: member(TARGET, ["usage:read", "cases:read"]),
            ADMIN_ID: member(ADMIN_ID, [], admin=True),
        }
        self.me_perms = me_perms
        self.calls: list[tuple[str, str, Any]] = []
        self.client = GateClient(
            "http://gate",
            "t",
            http=httpx.AsyncClient(
                transport=httpx.MockTransport(self.handle), base_url="http://gate"
            ),
        )

    def error(self, status: int, code: str) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": code, "message": "x"}})

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        parts = request.url.path.strip("/").split("/")
        if parts == ["v1", "staff", "me"]:
            actor = self.members[ME]
            held = self.me_perms if not actor["is_admin"] else sorted(GRANTABLE_PERMISSIONS_STR)
            return httpx.Response(200, json={**actor, "actor_permissions": held})
        if parts == ["v1", "staff"]:
            return httpx.Response(200, json={"items": list(self.members.values())})
        target = self.members.get(int(parts[2])) if len(parts) > 2 else None
        if request.method == "GET":
            return httpx.Response(200, json=target) if target else self.error(404, "TWR-STF-001")
        if request.method == "DELETE":
            return httpx.Response(204) if target else self.error(404, "TWR-STF-001")
        if parts[-1] == "permissions":
            assert target is not None
            target["permissions"] = target["effective_permissions"] = (body or {})["permissions"]
            return httpx.Response(200, json=target)
        new = member(int(parts[2]), [], name=(body or {}).get("display_name"))
        self.members[int(parts[2])] = new
        return httpx.Response(200, json=new)

    def writes(self) -> list[tuple[str, str, Any]]:
        return [c for c in self.calls if c[0] in ("PUT", "DELETE")]


def selects(reply: Reply) -> dict[str, Select]:
    return {f.control.custom_id: f.control for f in reply.fields if isinstance(f.control, Select)}


# --- catalogue and pure rules --------------------------------------------------------
def test_catalogue_covers_exactly_the_grantable_permissions() -> None:
    assert h.ALL_PERMISSIONS == GRANTABLE_PERMISSIONS_STR
    assert len([p for c in h.CATEGORIES for p in c.permissions]) == len(h.ALL_PERMISSIONS)


@pytest.mark.parametrize("locale", ["en", "fr"])
def test_every_permission_command_and_category_is_translated(locale: str) -> None:
    for cat in h.CATEGORIES:
        assert has(locale, f"team.cat.{cat.id}")
        for p in cat.permissions:
            for kind in ("label", "desc"):
                text = CATALOGS[locale][f"team.perm.{p}.{kind}"]
                assert 0 < len(text) <= 100
    for area, commands in h.COMMAND_AREAS:
        assert has(locale, f"help.area.{area}")
        for command in commands:
            assert has(locale, f"help.cmd.{command}")


def test_can_act_on_hierarchy() -> None:
    assert h.can_act_on(10, 5, False, False, False)
    assert not h.can_act_on(5, 5, False, False, False)
    assert not h.can_act_on(3, 5, False, False, False)
    assert h.can_act_on(1, None, False, False, False)  # target left the guild
    assert not h.can_act_on(None, 0, False, False, False)
    assert h.can_act_on(1, 50, True, False, False)  # administrators bypass
    assert not h.can_act_on(99, 0, False, True, False)  # nobody but an admin acts on one
    assert h.can_act_on(1, 50, False, False, True)  # yourself


def test_new_permissions_ignores_forged_values() -> None:
    new = h.new_permissions(
        current={"cases:read", "usage:read"},
        options=("cases:read", "cases:create"),
        selected=["cases:create", "staff:manage"],
    )
    assert new == {"usage:read", "cases:create"}


def test_command_map_unknown_command_is_shown() -> None:
    assert h.command_allowed("brand-new", set())
    assert not h.command_allowed("rank", {"staff:read"})
    assert h.command_allowed("rank", {"staff:manage"})
    assert h.command_allowed("help", set())


# --- /help ---------------------------------------------------------------------------
@pytest.mark.parametrize("locale", ["en", "fr"])
async def test_help_lists_only_usable_commands(locale: str) -> None:
    fake = FakeGate(["watchlist:read", "usage:read"])
    reply = await h.help_card(fake.client, ME, locale)
    assert "/watchlist list" in reply.text and "/usage" in reply.text
    assert "/watchlist add" not in reply.text and "/rank" not in reply.text
    assert "/help" in reply.text


async def test_help_for_administrator_fits_one_message() -> None:
    for locale in ("en", "fr"):
        fake = FakeGate([], me_admin=True)
        reply = await h.help_card(fake.client, ME, locale)
        assert "/unrank" in reply.text and "/registry set-control" in reply.text
        assert sum(len(i) for i in reply.items) < 3600


# --- /team ---------------------------------------------------------------------------
async def test_team_list_has_a_persistent_select() -> None:
    fake = FakeGate(["staff:read"])
    reply = await h.team_list(fake.client, ME, "en")
    assert len(reply.items) == 3
    assert any("<@300>" in i and "owner" in i for i in reply.items)
    assert_persistent(reply)
    select = selects(reply)[h.CID_TEAM_OPEN]
    assert [o.value for o in select.options] == ["100", "200", "300"]


async def test_team_list_denied_is_an_error_card() -> None:
    class Denied(FakeGate):
        def handle(self, request: httpx.Request) -> httpx.Response:
            return self.error(403, "TWR-AUTH-005")

    reply = await h.team_list(Denied([]).client, ME, "fr")
    assert reply.accent == "warning" and "TWR-AUTH-005" in reply.footer


# --- /rank and /unrank ---------------------------------------------------------------
async def test_rank_success_has_manage_permissions_button() -> None:
    fake = FakeGate(["staff:manage"])
    reply = await h.rank(fake.client, ME, "en", 400, "Newbie", actor_top=10, target_top=2)
    assert reply.accent == "success"
    assert reply.buttons[0].custom_id == h.cid_perms(400)
    assert_persistent(reply)
    assert ("PUT", "/v1/staff/400", {"display_name": "Newbie"}) in fake.writes()


async def test_rank_refused_by_hierarchy_never_calls_gate() -> None:
    fake = FakeGate(["staff:manage"])
    reply = await h.rank(fake.client, ME, "en", 400, "X", actor_top=2, target_top=5)
    assert reply.accent == "warning" and not fake.writes()
    admin = await h.rank(fake.client, ME, "en", ADMIN_ID, "X", actor_top=99, target_top=0)
    assert admin.accent == "warning" and not fake.writes()


async def test_unrank_confirmation_then_removal() -> None:
    fake = FakeGate(["staff:manage", "staff:read"])
    ask = await h.unrank_confirm(fake.client, ME, "en", TARGET, 10, 2)
    assert ask.accent == "warning" and len(ask.buttons) == 2
    assert_persistent(ask)
    assert not fake.writes()
    done = await h.unrank_do(fake.client, ME, "en", TARGET, 10, 2)
    assert done.accent == "success" and ("DELETE", f"/v1/staff/{TARGET}", None) in fake.writes()
    blocked = await h.unrank_do(fake.client, ME, "en", TARGET, 1, 2)
    assert blocked.accent == "warning" and len(fake.writes()) == 1
    cancelled = await h.unrank_cancelled(fake.client, ME, "en")
    assert cancelled.accent == "neutral"


async def test_unrank_of_unknown_member_shows_the_gate_error() -> None:
    fake = FakeGate(["staff:manage"])
    reply = await h.unrank_confirm(fake.client, ME, "en", 999, 10, 0)
    assert "TWR-STF-001" in reply.footer


# --- /permissions --------------------------------------------------------------------
async def test_panel_for_administrator_offers_every_category() -> None:
    fake = FakeGate([], me_admin=True)
    reply = await h.permissions_panel(fake.client, ME, "en", TARGET, 1, 1)
    assert_persistent(reply)
    sel = selects(reply)
    assert len(sel) == len(h.CATEGORIES) and not any(s.disabled for s in sel.values())
    cases = sel[h.cid_perm_set(TARGET, "cases")]
    assert {o.value for o in cases.options if o.default} == {"cases:read"}
    assert len(cases.options) == 5 and cases.min_values == 0
    assert len(reply.fields) + len(reply.buttons) + 4 < 40


async def test_panel_offers_only_what_the_actor_holds_and_locks_the_rest() -> None:
    mine = ["permissions:manage_others", "usage:read", "errors:read"]
    fake = FakeGate(mine)
    reply = await h.permissions_panel(fake.client, ME, "fr", TARGET, 10, 2)
    sel = selects(reply)
    supervision = sel[h.cid_perm_set(TARGET, "supervision")]
    assert [o.value for o in supervision.options] == ["usage:read", "errors:read"]
    assert h.cid_perm_set(TARGET, "cases") not in sel  # nothing to offer there...
    assert any("cases" in i.lower() or "case" in i.lower() for i in reply.items)  # ...locked
    assert_persistent(reply)


async def test_panel_is_read_only_without_permission_or_hierarchy() -> None:
    fake = FakeGate(["staff:read"])
    reply = await h.permissions_panel(fake.client, ME, "en", TARGET, 10, 2)
    assert all(s.disabled for s in selects(reply).values())
    assert reply.buttons[1].disabled
    fake2 = FakeGate([], me_admin=False)
    fake2.me_perms = ["permissions:manage_others"]
    low = await h.permissions_panel(fake2.client, ME, "en", TARGET, 2, 5)
    assert all(s.disabled for s in selects(low).values())
    admin_target = await h.permissions_panel(fake2.client, ME, "en", ADMIN_ID, 99, 0)
    assert all(s.disabled for s in selects(admin_target).values())
    assert "owner" in admin_target.text.lower() or admin_target.body


async def test_panel_for_self_uses_manage_self() -> None:
    fake = FakeGate(["permissions:manage_self", "usage:read"])
    reply = await h.permissions_panel(fake.client, ME, "en", ME, None, None)
    assert not any(s.disabled for s in selects(reply).values())


async def test_set_category_computes_new_list_and_rerenders() -> None:
    fake = FakeGate([], me_admin=True)
    reply = await h.set_category(
        fake.client,
        ME,
        "en",
        TARGET,
        "cases",
        ["cases:create", "cases:review"],
        1,
        1,
    )
    puts = [c for c in fake.writes() if c[1].endswith("/permissions")]
    # the "usage:read" permission, outside the category, is kept
    assert puts[0][2] == {"permissions": ["cases:create", "cases:review", "usage:read"]}
    assert reply.footer and "cases:create" in str(
        [o.value for s in selects(reply).values() for o in s.options if o.default]
    )
    assert_persistent(reply)


async def test_set_category_ignores_values_the_actor_may_not_change() -> None:
    fake = FakeGate(["permissions:manage_others", "usage:read"])
    await h.set_category(
        fake.client, ME, "en", TARGET, "supervision", ["staff:manage", "errors:manage"], 10, 2
    )
    puts = [c for c in fake.writes() if c[1].endswith("/permissions")]
    # usage:read (held by the actor, offered) is removed; nothing forged is added
    assert puts[0][2] == {"permissions": ["cases:read"]}


async def test_set_category_without_change_writes_nothing() -> None:
    fake = FakeGate([], me_admin=True)
    reply = await h.set_category(fake.client, ME, "en", TARGET, "cases", ["cases:read"], 1, 1)
    assert not fake.writes() and not reply.footer


async def test_set_category_hierarchy_and_unknown_category() -> None:
    fake = FakeGate(["permissions:manage_others", "usage:read"])
    low = await h.set_category(fake.client, ME, "en", TARGET, "supervision", [], 1, 5)
    assert low.accent == "warning" and not fake.writes()
    bad = await h.set_category(fake.client, ME, "en", TARGET, "nope", [], 10, 2)
    assert bad.accent == "warning" and not fake.writes()


async def test_clear_all_removes_only_what_the_actor_may_change() -> None:
    fake = FakeGate(["permissions:manage_others", "usage:read"])
    await h.clear_permissions(fake.client, ME, "en", TARGET, 10, 2)
    puts = [c for c in fake.writes() if c[1].endswith("/permissions")]
    assert puts[0][2] == {"permissions": ["cases:read"]}
    admin = FakeGate([], me_admin=True)
    await h.clear_permissions(admin.client, ME, "en", TARGET, 1, 1)
    sent = next(c for c in admin.writes() if c[1].endswith("/permissions"))
    assert sent[2] == {"permissions": []}


async def test_clear_confirmation_is_persistent_and_in_place() -> None:
    fake = FakeGate([], me_admin=True)
    reply = await h.permissions_panel(fake.client, ME, "en", TARGET, 1, 1, confirm_clear=True)
    assert reply.accent == "warning" and len(reply.buttons) == 2
    assert_persistent(reply)
    assert not fake.writes()


# --- client --------------------------------------------------------------------------
async def test_gate_client_team_requests() -> None:
    fake = FakeGate(["staff:read"])
    c = fake.client
    assert (await c.staff_me(ME))["is_admin"] is False
    assert len(await c.list_staff(ME, include_inactive=True)) == 3
    assert (await c.get_staff(ME, "200"))["discord_id"] == "200"
    await c.rank_staff(ME, 500, display_name="N")
    await c.set_staff_permissions(ME, 200, ["b", "a", "a"])
    await c.unrank_staff(ME, 200)
    assert ("PUT", "/v1/staff/200/permissions", {"permissions": ["a", "b"]}) in fake.writes()
    assert ("DELETE", "/v1/staff/200", None) in fake.writes()
