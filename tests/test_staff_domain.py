"""Pure rules of team ranks and individual permissions (no database)."""

from __future__ import annotations

import pytest

from tower.core.config import Settings
from tower.core.domain import error_codes as ec
from tower.core.domain.errors import InvalidInput, PermissionDenied
from tower.core.domain.security import (
    GRANTABLE_PERMISSIONS,
    ROLE_PERMISSIONS,
    Actor,
    Permission,
    Role,
    member_permissions,
    parse_grantable,
    permissions_change_rules,
)

BOT = Actor.service("intercom", frozenset(Permission))
P = Permission


def test_grantable_excludes_service_and_public_permissions() -> None:
    assert not GRANTABLE_PERMISSIONS & {
        P.CASE_READ_PUBLIC,
        P.CONTENT_INGEST,
        P.EVENTS_READ,
    }
    assert {P.STAFF_READ, P.PERMISSIONS_MANAGE_OTHERS, P.PERMISSIONS_MANAGE_SELF} <= (
        GRANTABLE_PERMISSIONS
    )


def test_staff_actor_unions_roles_and_individual_permissions() -> None:
    actor = Actor.staff(1, frozenset({Role.REVIEWER}), BOT, frozenset({P.WATCHLIST_WRITE}))
    assert actor.can(P.CASE_REVIEW) and actor.can(P.WATCHLIST_WRITE)
    assert not actor.can(P.CASE_DECIDE)


def test_hard_coded_admin_has_everything_but_stays_bound_to_service_scopes() -> None:
    admin = Actor.staff(1, frozenset(), BOT, is_admin=True)
    assert all(admin.can(p) for p in Permission)
    narrow = Actor.service("narrow", frozenset({P.STAFF_READ}))
    scoped = Actor.staff(1, frozenset(), narrow, is_admin=True)
    assert scoped.permissions == frozenset({P.STAFF_READ, P.CASE_READ_PUBLIC})


def test_member_without_anything_only_gets_public_permissions() -> None:
    actor = Actor.staff(2, frozenset(), BOT)
    assert actor.permissions == frozenset({P.CASE_READ_PUBLIC})


def test_member_permissions_presets_individual_and_admin() -> None:
    assert member_permissions(["reviewer"], [], is_admin=False) == (
        ROLE_PERMISSIONS[Role.REVIEWER] & GRANTABLE_PERMISSIONS
    )
    assert member_permissions([], ["usage:read", "bogus", "events:read"], is_admin=False) == {
        P.USAGE_READ
    }
    assert member_permissions([], [], is_admin=True) == GRANTABLE_PERMISSIONS


def test_parse_grantable_rejects_unknown_and_non_grantable() -> None:
    assert parse_grantable(["usage:read", "usage:read"]) == {P.USAGE_READ}
    for bad in ("nope", "events:read", "cases:read_public", "content:ingest"):
        with pytest.raises(InvalidInput) as err:
            parse_grantable([bad])
        assert err.value.error_code is ec.STF_BAD_PERMISSION


def test_admin_actor_may_change_anything() -> None:
    added, removed = permissions_change_rules(
        actor_holds=frozenset(),
        actor_is_admin=True,
        current=frozenset({P.USAGE_READ}),
        new=frozenset({P.ERRORS_READ}),
    )
    assert added == {P.ERRORS_READ} and removed == {P.USAGE_READ}


def test_non_admin_cannot_escalate() -> None:
    holds = frozenset({P.USAGE_READ, P.ERRORS_READ})
    with pytest.raises(PermissionDenied) as err:
        permissions_change_rules(
            actor_holds=holds,
            actor_is_admin=False,
            current=frozenset(),
            new=frozenset({P.USAGE_READ, P.STAFF_MANAGE}),
        )
    assert err.value.error_code is ec.STF_ESCALATION
    assert err.value.details["permissions"] == ["staff:manage"]


def test_non_admin_cannot_remove_what_he_does_not_hold() -> None:
    with pytest.raises(PermissionDenied):
        permissions_change_rules(
            actor_holds=frozenset({P.USAGE_READ}),
            actor_is_admin=False,
            current=frozenset({P.STAFF_MANAGE}),
            new=frozenset(),
        )


def test_non_admin_may_touch_what_he_holds_and_keep_the_rest() -> None:
    added, removed = permissions_change_rules(
        actor_holds=frozenset({P.USAGE_READ, P.ERRORS_READ}),
        actor_is_admin=False,
        current=frozenset({P.STAFF_MANAGE, P.USAGE_READ}),
        new=frozenset({P.STAFF_MANAGE, P.ERRORS_READ}),
    )
    assert added == {P.ERRORS_READ} and removed == {P.USAGE_READ}


def test_admin_ids_setting_parses_comma_separated_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOWER_ADMIN_DISCORD_IDS", "123, 456,")
    assert Settings().admin_discord_ids == [123, 456]
    monkeypatch.delenv("TOWER_ADMIN_DISCORD_IDS")
    assert Settings().admin_discord_ids == []


def test_staff_error_codes_are_in_the_catalog() -> None:
    codes = [c for c in ec.CATALOG if c.startswith("TWR-STF-")]
    assert codes[:6] == [f"TWR-STF-00{i}" for i in range(1, 7)]
    assert ec.STF_NOT_RANKED.http_status == 404
