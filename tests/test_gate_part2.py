from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain.llm import month_period
from tower.core.domain.security import Permission
from tower.core.llm.admin import _seed, run_budget, run_seed, run_usage

from .conftest import TokenFactory

STAFF = 333333333333333333


def h(token: str | None = None, staff: int | None = None) -> dict[str, str]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if staff:
        headers["X-Acting-Discord-Id"] = str(staff)
    return headers


# --- SSE cursors ------------------------------------------------------------------------


async def test_cursor_get_before_put_then_roundtrip(
    client: httpx.AsyncClient, make_token: TokenFactory
) -> None:
    token = await make_token("intercom")
    r = await client.get("/v1/events/cursors/main", headers=h(token))
    assert r.status_code == 200
    assert r.json() == {"name": "main", "cursor": None, "updated_at": None}

    r = await client.put("/v1/events/cursors/main", json={"cursor": "12-34"}, headers=h(token))
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "main" and body["cursor"] == "12-34" and body["updated_at"]

    r = await client.get("/v1/events/cursors/main", headers=h(token))
    assert r.json()["cursor"] == "12-34"

    r = await client.put("/v1/events/cursors/main", json={"cursor": "0"}, headers=h(token))
    assert r.json()["cursor"] == "0-0"


async def test_cursor_isolated_between_services(
    client: httpx.AsyncClient, make_token: TokenFactory, db: Database
) -> None:
    a = await make_token("intercom")
    b = await make_token("radar")
    await client.put("/v1/events/cursors/main", json={"cursor": "1-2"}, headers=h(a))
    assert (await client.get("/v1/events/cursors/main", headers=h(b))).json()["cursor"] is None
    await client.put("/v1/events/cursors/main", json={"cursor": "5-6"}, headers=h(b))
    assert (await client.get("/v1/events/cursors/main", headers=h(a))).json()["cursor"] == "1-2"
    async with db.session() as session:
        keys = (
            (await session.execute(text("SELECT consumer FROM event_cursors ORDER BY consumer")))
            .scalars()
            .all()
        )
    assert list(keys) == ["sse:intercom:main", "sse:radar:main"]


async def test_cursor_staff_via_service_uses_service_key(
    client: httpx.AsyncClient,
    make_token: TokenFactory,
    add_staff: Callable[..., Coroutine[Any, Any, None]],
) -> None:
    token = await make_token("intercom")
    # editors do not hold events:read; admins (all permissions) do.
    await add_staff(STAFF, ["admin"])
    await client.put("/v1/events/cursors/main", json={"cursor": "7-8"}, headers=h(token))
    r = await client.get("/v1/events/cursors/main", headers=h(token, STAFF))
    assert r.status_code == 200 and r.json()["cursor"] == "7-8"
    r = await client.put("/v1/events/cursors/main", json={"cursor": "9-9"}, headers=h(token, STAFF))
    assert r.status_code == 200
    assert (await client.get("/v1/events/cursors/main", headers=h(token))).json()["cursor"] == "9-9"


async def test_cursor_validation_and_permissions(
    client: httpx.AsyncClient, make_token: TokenFactory
) -> None:
    token = await make_token("intercom")
    r = await client.get("/v1/events/cursors/Bad Name", headers=h(token))
    assert r.status_code == 422 and r.json()["error"]["code"] == "TWR-EVT-003"
    r = await client.put("/v1/events/cursors/" + "a" * 65, json={"cursor": "1-1"}, headers=h(token))
    assert r.status_code == 422 and r.json()["error"]["code"] == "TWR-EVT-003"
    r = await client.put("/v1/events/cursors/main", json={"cursor": "nope"}, headers=h(token))
    assert r.status_code == 422 and r.json()["error"]["code"] == "TWR-EVT-001"

    limited = await make_token("limited", scopes=[Permission.DOSSIER_READ])
    r = await client.get("/v1/events/cursors/main", headers=h(limited))
    assert r.status_code == 403
    r = await client.put("/v1/events/cursors/main", json={"cursor": "1-1"}, headers=h(limited))
    assert r.status_code == 403

    r = await client.get("/v1/events/cursors/main")
    assert r.status_code == 403
    r = await client.put("/v1/events/cursors/main", json={"cursor": "1-1"})
    assert r.status_code == 403


async def test_cursor_writes_no_event(
    client: httpx.AsyncClient, make_token: TokenFactory, db: Database
) -> None:
    token = await make_token("intercom")
    await client.put("/v1/events/cursors/main", json={"cursor": "1-1"}, headers=h(token))
    async with db.session() as session:
        assert (await session.execute(text("SELECT count(*) FROM events"))).scalar_one() == 0


# --- LLM usage --------------------------------------------------------------------------


async def seed_usage(db: Database) -> None:
    start, end = month_period(datetime.now(UTC))
    async with db.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO llm_free_usage (provider, free_group, day, tokens)"
                " VALUES ('openai', 'small', (now() AT TIME ZONE 'UTC')::date, 250000)"
            )
        )
        await session.execute(
            text(
                "INSERT INTO budget_counters (scope, period_start, period_end, cap_usd_micros,"
                " spent_usd_micros, reserved_usd_micros) VALUES"
                " ('provider:x', :s, :e, 20000000, 5, 6), ('global', :s, :e, 30000000, 1, 2)"
            ),
            {"s": start, "e": end},
        )


async def test_usage_values_editor_ok_reviewer_forbidden(
    client: httpx.AsyncClient,
    make_token: TokenFactory,
    add_staff: Callable[..., Coroutine[Any, Any, None]],
    db: Database,
    settings: Settings,
) -> None:
    await seed_usage(db)
    token = await make_token("intercom")
    await add_staff(STAFF, ["editor"])
    await add_staff(STAFF + 1, ["reviewer"])

    r = await client.get("/v1/llm/usage", headers=h(token, STAFF))
    assert r.status_code == 200
    body = r.json()
    start, end = month_period(datetime.now(UTC))
    assert body["usage_tier"] == settings.openai_usage_tier
    assert body["period_start"] == start.isoformat() and body["period_end"] == end.isoformat()
    free = {(f["provider"], f["free_group"]): f for f in body["free"]}
    assert free[("openai", "small")]["used_tokens"] == 250_000
    assert free[("openai", "small")]["allowance_tokens"] > 0
    assert free[("openai", "large")]["used_tokens"] == 0
    assert body["scopes"] == [
        {
            "scope": "global",
            "spent_usd_micros": 1,
            "reserved_usd_micros": 2,
            "cap_usd_micros": 30_000_000,
        },
        {
            "scope": "provider:x",
            "spent_usd_micros": 5,
            "reserved_usd_micros": 6,
            "cap_usd_micros": 20_000_000,
        },
    ]

    r = await client.get("/v1/llm/usage", headers=h(token, STAFF + 1))
    assert r.status_code == 403
    assert (await client.get("/v1/llm/usage")).status_code == 403


async def test_run_usage_still_prints(
    db: Database, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    await seed_usage(db)
    await run_usage(settings)
    out = capsys.readouterr().out
    assert "Free allocation" in out and "openai/small" in out and "10.0%" in out
    assert "openai/large" in out
    assert "global" in out and "$30.00" in out and "provider:x" in out


async def test_run_budget_creates_updates_and_validates(
    db: Database, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    await run_budget(settings, "provider:x", 12.5)
    assert "$12.50" in capsys.readouterr().out
    await run_budget(settings, "provider:x", 7)
    await run_budget(settings, "global", 0)
    async with db.session() as session:
        rows = (
            await session.execute(
                text("SELECT scope, cap_usd_micros FROM budget_counters ORDER BY scope")
            )
        ).all()
    assert [(r.scope, r.cap_usd_micros) for r in rows] == [("global", 0), ("provider:x", 7_000_000)]
    for bad_scope, usd in (("Bad Scope", 1.0), ("x:", 1.0), ("provider:x", -1.0)):
        with pytest.raises(ValueError, match=r"Invalid scope|Cap must"):
            await run_budget(settings, bad_scope, usd)


async def test_seed_provider_x_counter_is_idempotent(
    db: Database, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    await run_seed(settings)
    assert "Provider X budget created: yes" in capsys.readouterr().out
    async with db.session() as session:
        cap = (
            await session.execute(
                text("SELECT cap_usd_micros FROM budget_counters WHERE scope = 'provider:x'")
            )
        ).scalar_one()
    assert cap == round(settings.x_monthly_cap_usd * 1_000_000)
    await run_budget(settings, "provider:x", 3)
    await run_seed(settings)
    assert "Provider X budget created: no" in capsys.readouterr().out
    async with db.session() as session:
        rows = (
            (
                await session.execute(
                    text("SELECT cap_usd_micros FROM budget_counters WHERE scope = 'provider:x'")
                )
            )
            .scalars()
            .all()
        )
    assert list(rows) == [3_000_000]
    assert callable(_seed)
