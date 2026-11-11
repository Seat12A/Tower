from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from tower.core.db.database import Database
from tower.core.domain.errors import Conflict, InvalidInput, NotFound, PermissionDenied
from tower.core.domain.security import Actor, Permission
from tower.core.usecases import watchlist as uc

from .conftest import TokenFactory

STAFF = 444444444444444444
EDITOR = Actor.system("test")
RADAR = Actor.system("radar")
READER = Actor.service("reader", frozenset({Permission.WATCHLIST_READ}))
XID = "44196397"


def h(token: str, staff: int | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if staff:
        headers["X-Acting-Discord-Id"] = str(staff)
    return headers


async def add(db: Database, **kw: Any) -> Any:
    kw.setdefault("platform", "x")
    kw.setdefault("category", "media_outlet")
    async with db.transaction() as session:
        return await uc.add_watched_account(session, EDITOR, **kw)


async def events(db: Database) -> list[dict[str, Any]]:
    async with db.session() as session:
        rows = (
            (
                await session.execute(
                    text("SELECT payload FROM events WHERE topic = 'watchlist.changed' ORDER BY id")
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


async def count(db: Database, sql: str) -> int:
    async with db.session() as session:
        return int((await session.execute(text(sql))).scalar_one())


async def test_add_by_handle_creates_pending(db: Database) -> None:
    w, a = await add(db, handle="@Jack")
    assert a.external_id is None and a.handle == "Jack"
    ev = await events(db)
    assert len(ev) == 1
    assert ev[0]["action"] == "added"
    assert ev[0]["watched_account"]["external_id"] is None
    assert ev[0]["watched_account"]["handle"] == "Jack"
    assert w.version == 1


async def test_add_same_handle_twice_conflicts_case_insensitive(db: Database) -> None:
    await add(db, handle="Jack")
    with pytest.raises(Conflict):
        await add(db, handle="jACK")
    assert await count(db, "SELECT count(*) FROM platform_accounts") == 1
    assert await count(db, "SELECT count(*) FROM watched_accounts") == 1


async def test_handle_of_known_account_reuses_it(db: Database) -> None:
    async with db.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO platform_accounts (platform, external_id, handle)"
                " VALUES ('x', :i, 'Known')"
            ),
            {"i": XID},
        )
    _, a = await add(db, handle="known")
    assert a.external_id == XID
    assert await count(db, "SELECT count(*) FROM platform_accounts") == 1
    assert (await events(db))[0]["watched_account"]["external_id"] == XID


async def test_add_requires_id_or_handle_and_valid_handle(db: Database) -> None:
    with pytest.raises(InvalidInput) as exc:
        await add(db)
    assert exc.value.code == "TWR-ING-006"
    with pytest.raises(InvalidInput):
        await add(db, handle="not a handle!")
    with pytest.raises(InvalidInput):
        await add(db, handle="a" * 16)


async def test_resolve_sets_id_and_publishes(db: Database) -> None:
    w, _ = await add(db, handle="Jack")
    async with db.transaction() as session:
        _, a2 = await uc.resolve_watched_account(
            session, RADAR, watched_public_id=str(w.public_id), external_id=XID, handle="jack"
        )
    assert a2.external_id == XID and a2.handle == "jack"
    ev = await events(db)
    assert [e["action"] for e in ev] == ["added", "resolved"]
    assert ev[1]["watched_account"]["external_id"] == XID
    assert (
        await count(db, "SELECT count(*) FROM watched_account_events WHERE action = 'resolved'")
        == 1
    )

    # Idempotent: same id again changes nothing and publishes nothing.
    async with db.transaction() as session:
        await uc.resolve_watched_account(
            session, RADAR, watched_public_id=str(w.public_id), external_id=XID, handle=None
        )
    assert len(await events(db)) == 2

    # A different id on a resolved account is a conflict.
    async with db.transaction() as session:
        with pytest.raises(Conflict):
            await uc.resolve_watched_account(
                session,
                RADAR,
                watched_public_id=str(w.public_id),
                external_id="123",
                handle=None,
            )


async def test_resolve_merges_into_existing_unwatched_account(db: Database) -> None:
    async with db.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO platform_accounts (platform, external_id, handle)"
                " VALUES ('x', :i, 'old')"
            ),
            {"i": XID},
        )
    w, pending = await add(db, handle="Jack")
    async with db.transaction() as session:
        _, a = await uc.resolve_watched_account(
            session, RADAR, watched_public_id=str(w.public_id), external_id=XID, handle="Jack"
        )
    assert a.external_id == XID and a.handle == "Jack"
    assert await count(db, "SELECT count(*) FROM platform_accounts") == 1
    assert pending.id != a.id
    assert await count(db, "SELECT count(*) FROM watched_accounts") == 1


async def test_resolve_conflicts_when_target_is_watched(db: Database) -> None:
    await add(db, external_id=XID, handle="other")
    w, _ = await add(db, handle="Jack")
    async with db.transaction() as session:
        with pytest.raises(Conflict) as exc:
            await uc.resolve_watched_account(
                session, RADAR, watched_public_id=str(w.public_id), external_id=XID, handle=None
            )
    assert exc.value.code == "TWR-ING-009"
    assert await count(db, "SELECT count(*) FROM platform_accounts") == 2


async def test_resolve_errors_and_permissions(db: Database) -> None:
    w, _ = await add(db, handle="Jack")
    async with db.transaction() as session:
        with pytest.raises(PermissionDenied):
            await uc.resolve_watched_account(
                session, READER, watched_public_id=str(w.public_id), external_id=XID, handle=None
            )
        with pytest.raises(NotFound):
            await uc.resolve_watched_account(
                session,
                RADAR,
                watched_public_id="00000000-0000-0000-0000-000000000000",
                external_id=XID,
                handle=None,
            )
        with pytest.raises(NotFound):
            await uc.resolve_watched_account(
                session, RADAR, watched_public_id="nope", external_id=XID, handle=None
            )
        with pytest.raises(InvalidInput):
            await uc.resolve_watched_account(
                session, RADAR, watched_public_id=str(w.public_id), external_id="abc", handle=None
            )


async def test_gate_add_by_handle_and_list(
    client: httpx.AsyncClient,
    make_token: TokenFactory,
    add_staff: Callable[..., Coroutine[Any, Any, None]],
) -> None:
    token = await make_token("intercom")
    await add_staff(STAFF, ["editor"])
    r = await client.post(
        "/v1/watched-accounts",
        json={"platform": "x", "handle": "@Jack", "category": "media_outlet"},
        headers=h(token, STAFF),
    )
    assert r.status_code == 201, r.text
    assert r.json()["external_id"] is None and r.json()["handle"] == "Jack"
    r = await client.get("/v1/watched-accounts", headers=h(token, STAFF))
    assert [w["external_id"] for w in r.json()] == [None]
    r = await client.post(
        "/v1/watched-accounts",
        json={"platform": "x", "handle": "jack", "category": "media_outlet"},
        headers=h(token, STAFF),
    )
    assert r.status_code == 409
    r = await client.post(
        "/v1/watched-accounts",
        json={"platform": "x", "category": "media_outlet"},
        headers=h(token, STAFF),
    )
    assert r.status_code == 422
