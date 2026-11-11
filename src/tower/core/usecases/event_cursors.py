"""Stored SSE cursors of service clients (the bot resumes its stream without loss).

Stored in ``event_cursors`` under ``sse:<service client>:<name>``: a staff member acting
through a service uses the service's cursors. No audit nor event: client reading state,
and an event would itself be streamed back. See docs/events.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain.security import Actor

__all__ = ["ClientCursor", "get_client_cursor", "set_client_cursor"]


@dataclass(frozen=True, slots=True)
class ClientCursor:
    name: str
    cursor: str | None
    """``<xid>-<id>`` (as in SSE ``id:``), None when never stored."""
    updated_at: datetime | None


async def get_client_cursor(session: AsyncSession, actor: Actor, name: str) -> ClientCursor:
    """Requires ``events:read`` and a service (or staff-via-service) actor."""
    raise NotImplementedError


async def set_client_cursor(
    session: AsyncSession, actor: Actor, name: str, cursor: str
) -> ClientCursor:
    """Upsert; validates the name (``TWR-EVT-003``) and the cursor (``TWR-EVT-001``)."""
    raise NotImplementedError
