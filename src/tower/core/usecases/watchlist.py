"""Watchlist: accounts followed by the team. Only changes surveillance, never verdicts."""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain import error_codes as ec
from tower.core.domain.errors import Conflict, InvalidInput, NotFound
from tower.core.domain.platforms import PublicRole, parse_platform, validate_external_id
from tower.core.domain.security import Actor, Permission
from tower.core.events.outbox import publish
from tower.core.models.ingestion import PlatformAccount, WatchedAccount, WatchedAccountEvent

AGGREGATE = "watched_account"
_MAX_HANDLE = 100
_MAX_NOTE = 2000


def watched_account_payload(w: WatchedAccount, a: PlatformAccount) -> dict[str, Any]:
    """Representation used in events and API responses (ids as strings)."""
    return {
        "public_id": str(w.public_id),
        "platform": a.platform,
        "external_id": a.external_id,
        "handle": a.handle,
        "category": w.category,
        "priority": w.priority,
        "active": w.active,
        "note": w.note,
        "version": w.version,
    }


def _category(value: str) -> str:
    try:
        return PublicRole(value).value
    except ValueError:
        raise InvalidInput("Unknown category", category=str(value)[:64]) from None


def _priority(value: int) -> int:
    if not -100 <= value <= 100:
        raise InvalidInput("Priority out of range (-100..100)", priority=value)
    return value


def _handle(value: str | None) -> str | None:
    if value is None:
        return None
    handle = value.strip().lstrip("@")
    if len(handle) > _MAX_HANDLE:
        raise InvalidInput("Handle too long", max_chars=_MAX_HANDLE)
    return handle or None


def _note(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) > _MAX_NOTE:
        raise InvalidInput("Note too long", max_chars=_MAX_NOTE)
    return value or None


async def list_watched_accounts(
    session: AsyncSession,
    actor: Actor,
    *,
    active: bool | None = None,
    platform: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> Sequence[tuple[WatchedAccount, PlatformAccount]]:
    actor.require(Permission.WATCHLIST_READ)
    stmt = (
        select(WatchedAccount, PlatformAccount)
        .join(PlatformAccount, PlatformAccount.id == WatchedAccount.account_id)
        .order_by(WatchedAccount.priority.desc(), WatchedAccount.id)
        .limit(min(max(limit, 1), 500))
        .offset(max(offset, 0))
    )
    if active is not None:
        stmt = stmt.where(WatchedAccount.active == active)
    if platform is not None:
        stmt = stmt.where(PlatformAccount.platform == parse_platform(platform).value)
    return [(w, a) for w, a in (await session.execute(stmt)).all()]


_X_HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")


async def _account_by_handle(session: AsyncSession, platform: str, handle: str) -> PlatformAccount:
    """Account of ``handle``: a resolved one if known, else the (created) pending one."""
    lowered = func.lower(PlatformAccount.handle) == handle.lower()
    known = await session.scalar(
        select(PlatformAccount)
        .where(
            PlatformAccount.platform == platform,
            lowered,
            PlatformAccount.external_id.is_not(None),
        )
        .order_by(PlatformAccount.id)
        .limit(1)
    )
    if known is not None:
        return known
    await session.execute(
        insert(PlatformAccount)
        .values(platform=platform, external_id=None, handle=handle)
        .on_conflict_do_nothing(
            index_elements=[PlatformAccount.platform, func.lower(PlatformAccount.handle)],
            index_where=PlatformAccount.external_id.is_(None),
        )
    )
    return (
        await session.execute(
            select(PlatformAccount).where(
                PlatformAccount.platform == platform,
                lowered,
                PlatformAccount.external_id.is_(None),
            )
        )
    ).scalar_one()


async def add_watched_account(
    session: AsyncSession,
    actor: Actor,
    *,
    platform: str,
    external_id: str | None = None,
    handle: str | None = None,
    category: str,
    priority: int = 0,
    note: str | None = None,
) -> tuple[WatchedAccount, PlatformAccount]:
    """Watch an account by native id, or by handle alone (pending until the radar
    resolves the id with ``resolve_watched_account``)."""
    actor.require(Permission.WATCHLIST_WRITE)
    plat = parse_platform(platform)
    handle = _handle(handle)
    if external_id is None and handle is None:
        raise InvalidInput(
            "external_id or handle is required", code=ec.ING_BAD_EXTERNAL_ID, platform=plat.value
        )
    if external_id is not None:
        validate_external_id(plat, external_id)
    elif handle is not None and plat.value == "x" and not _X_HANDLE.fullmatch(handle):
        raise InvalidInput("Invalid X handle", code=ec.ING_BAD_EXTERNAL_ID, handle=handle[:64])
    category = _category(category)
    priority = _priority(priority)
    note = _note(note)

    if external_id is not None:
        stmt = insert(PlatformAccount).values(
            platform=plat.value, external_id=external_id, handle=handle
        )
        account = (
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[PlatformAccount.platform, PlatformAccount.external_id],
                    set_={
                        "handle": func.coalesce(stmt.excluded.handle, PlatformAccount.handle),
                        "updated_at": func.now(),
                    },
                ).returning(PlatformAccount)
            )
        ).scalar_one()
        await session.refresh(account)
    elif handle is not None:
        account = await _account_by_handle(session, plat.value, handle)
    else:  # unreachable: checked above
        raise InvalidInput("external_id or handle is required", code=ec.ING_BAD_EXTERNAL_ID)

    existing = await session.scalar(
        select(WatchedAccount.id).where(WatchedAccount.account_id == account.id)
    )
    exists = Conflict(
        "Account is already on the watchlist",
        code=ec.ING_WATCHED_EXISTS,
        platform=plat.value,
        external_id=account.external_id,
        handle=account.handle,
    )
    if existing is not None:
        raise exists
    watched = WatchedAccount(
        account_id=account.id,
        category=category,
        priority=priority,
        note=note,
        added_by=actor.label,
    )
    try:
        async with session.begin_nested():
            session.add(watched)
            await session.flush()
    except IntegrityError:
        raise exists from None
    await session.refresh(watched)
    payload = watched_account_payload(watched, account)
    session.add(
        WatchedAccountEvent(
            watched_account_id=watched.id,
            action="added",
            actor=actor.label,
            changes={k: payload[k] for k in ("category", "priority", "note", "handle")},
        )
    )
    await _publish(session, actor, "added", watched, payload)
    return watched, account


async def resolve_watched_account(
    session: AsyncSession,
    actor: Actor,
    *,
    watched_public_id: str,
    external_id: str,
    handle: str | None,
) -> tuple[WatchedAccount, PlatformAccount]:
    """Attach the native id found by the radar to an account added by handle."""
    actor.require(Permission.WATCHLIST_WRITE)
    try:
        public_id = uuid.UUID(watched_public_id)
    except ValueError:
        raise NotFound(
            "Watched account not found",
            code=ec.ING_WATCHED_NOT_FOUND,
            public_id=str(watched_public_id)[:64],
        ) from None
    row = (
        await session.execute(
            select(WatchedAccount, PlatformAccount)
            .join(PlatformAccount, PlatformAccount.id == WatchedAccount.account_id)
            .where(WatchedAccount.public_id == public_id)
            .with_for_update()
        )
    ).first()
    if row is None:
        raise NotFound(
            "Watched account not found", code=ec.ING_WATCHED_NOT_FOUND, public_id=str(public_id)
        )
    watched, account = row
    if account.external_id is not None:
        if account.external_id == external_id:
            return watched, account
        raise Conflict(
            "Watched account already has a different external_id",
            public_id=str(public_id),
            current=account.external_id,
        )
    validate_external_id(account.platform, external_id)
    new_handle = _handle(handle)

    other = await session.scalar(
        select(PlatformAccount)
        .where(
            PlatformAccount.platform == account.platform,
            PlatformAccount.external_id == external_id,
        )
        .with_for_update()
    )
    if other is not None:
        if await session.scalar(
            select(WatchedAccount.id).where(WatchedAccount.account_id == other.id)
        ):
            raise Conflict(
                "Account is already on the watchlist",
                code=ec.ING_WATCHED_EXISTS,
                platform=account.platform,
                external_id=external_id,
            )
        pending_id = account.id
        watched.account_id = other.id
        if new_handle is not None:
            other.handle = new_handle
        other.updated_at = func.now()
        await session.flush()
        await session.execute(delete(PlatformAccount).where(PlatformAccount.id == pending_id))
        account = other
    else:
        account.external_id = external_id
        if new_handle is not None:
            account.handle = new_handle
        account.updated_at = func.now()
        await session.flush()
    watched.updated_at = func.now()
    await session.flush()
    await session.refresh(watched)
    await session.refresh(account)
    payload = watched_account_payload(watched, account)
    session.add(
        WatchedAccountEvent(
            watched_account_id=watched.id,
            action="resolved",
            actor=actor.label,
            changes={"external_id": account.external_id, "handle": account.handle},
        )
    )
    await _publish(session, actor, "resolved", watched, payload)
    return watched, account


async def update_watched_account(
    session: AsyncSession,
    actor: Actor,
    public_id: uuid.UUID,
    *,
    version: int,
    category: str | None = None,
    priority: int | None = None,
    active: bool | None = None,
    note: str | None = None,
    handle: str | None = None,
) -> tuple[WatchedAccount, PlatformAccount]:
    """Update with optimistic locking. ``note=""`` clears the note."""
    actor.require(Permission.WATCHLIST_WRITE)
    row = (
        await session.execute(
            select(WatchedAccount, PlatformAccount)
            .join(PlatformAccount, PlatformAccount.id == WatchedAccount.account_id)
            .where(WatchedAccount.public_id == public_id)
            .with_for_update(of=WatchedAccount)
        )
    ).first()
    if row is None:
        raise NotFound(
            "Watched account not found", code=ec.ING_WATCHED_NOT_FOUND, public_id=str(public_id)
        )
    watched, account = row
    if watched.version != version:
        raise Conflict(
            "The watched account was modified in the meantime",
            expected=version,
            current=watched.version,
        )
    changes: dict[str, Any] = {}

    def change(obj: Any, field: str, new: Any) -> None:
        old = getattr(obj, field)
        if old != new:
            changes[field] = {"from": old, "to": new}
            setattr(obj, field, new)

    if category is not None:
        change(watched, "category", _category(category))
    if priority is not None:
        change(watched, "priority", _priority(priority))
    if active is not None:
        change(watched, "active", active)
    if note is not None:
        change(watched, "note", _note(note))
    new_handle = _handle(handle)
    if new_handle is not None:
        change(account, "handle", new_handle)
    if not changes:
        return watched, account
    watched.updated_at = func.now()
    await session.flush()
    await session.refresh(watched)
    await session.refresh(account)
    payload = watched_account_payload(watched, account)
    session.add(
        WatchedAccountEvent(
            watched_account_id=watched.id,
            action="updated",
            actor=actor.label,
            changes=changes,
        )
    )
    await _publish(session, actor, "updated", watched, payload)
    return watched, account


async def _publish(
    session: AsyncSession,
    actor: Actor,
    action: str,
    watched: WatchedAccount,
    payload: dict[str, Any],
) -> None:
    await publish(
        session,
        "watchlist.changed",
        {"action": action, "watched_account": payload},
        aggregate_type=AGGREGATE,
        aggregate_id=str(watched.public_id),
        actor=actor.label,
    )
