"""Stored SSE cursors of service clients (the bot resumes its stream without loss).

Stored in ``event_cursors`` under ``sse:<service client>:<name>``: a staff member acting
through a service uses the service's cursors. No audit nor event: client reading state,
and an event would itself be streamed back. See docs/events.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain import error_codes as ec
from tower.core.domain.errors import InvalidInput, PermissionDenied
from tower.core.domain.security import Actor, ActorKind, Permission
from tower.core.events.outbox import Cursor
from tower.core.models.events import EventCursor

__all__ = ["ClientCursor", "get_client_cursor", "set_client_cursor"]


_NAME = re.compile(r"^[a-z0-9_.-]{1,64}$")


def _consumer(actor: Actor, name: str) -> str:
    actor.require(Permission.EVENTS_READ)
    if actor.kind is ActorKind.SERVICE and actor.id:
        service = actor.id
    elif actor.kind is ActorKind.STAFF and actor.via:
        service = actor.via
    else:
        raise PermissionDenied("Event cursors require a service client")
    if not _NAME.fullmatch(name):
        raise InvalidInput(
            "Invalid cursor name (a-z, 0-9, _ . -, 1 to 64 characters)",
            code=ec.EVT_BAD_CURSOR_NAME,
            name=name[:64],
        )
    return f"sse:{service}:{name}"


@dataclass(frozen=True, slots=True)
class ClientCursor:
    name: str
    cursor: str | None
    """``<xid>-<id>`` (as in SSE ``id:``), None when never stored."""
    updated_at: datetime | None


async def get_client_cursor(session: AsyncSession, actor: Actor, name: str) -> ClientCursor:
    """Requires ``events:read`` and a service (or staff-via-service) actor."""
    consumer = _consumer(actor, name)
    row = await session.scalar(select(EventCursor).where(EventCursor.consumer == consumer))
    if row is None:
        return ClientCursor(name, None, None)
    return ClientCursor(name, Cursor(row.last_xid, row.last_id).encode(), row.updated_at)


async def set_client_cursor(
    session: AsyncSession, actor: Actor, name: str, cursor: str
) -> ClientCursor:
    """Upsert; validates the name (``TWR-EVT-003``) and the cursor (``TWR-EVT-001``)."""
    consumer = _consumer(actor, name)
    parsed = Cursor.decode(cursor)
    stmt = insert(EventCursor).values(
        consumer=consumer, last_xid=parsed.xid, last_id=parsed.id, updated_at=func.now()
    )
    row = (
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[EventCursor.consumer],
                set_={
                    "last_xid": stmt.excluded.last_xid,
                    "last_id": stmt.excluded.last_id,
                    "updated_at": func.now(),
                },
            ).returning(EventCursor.updated_at)
        )
    ).scalar_one()
    return ClientCursor(name, parsed.encode(), row)
