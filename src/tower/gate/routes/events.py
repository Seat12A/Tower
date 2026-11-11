"""Server-Sent Events stream, for the bot and internal clients.

- Resume: ``Last-Event-ID`` header (sent automatically by EventSource) or the ``after``
  parameter. Without a cursor, the stream starts at the end (``after=latest``) or at the
  beginning (``after=0``).
- Woken by the process's shared LISTEN connection: no SQL connection held per client,
  one short session per batch.
- Regular heartbeat (SSE comment) to keep the connection open through the Railway proxy.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated

import orjson
from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from tower.core.domain.security import Permission
from tower.core.events.channels import EVENTS
from tower.core.events.outbox import Cursor, read_events
from tower.core.usecases import event_cursors as cursors_uc
from tower.gate.deps import ActorDep, DbDep, ReadSession, WriteSession

router = APIRouter(prefix="/v1/events", tags=["events"])

_LATEST = text(
    """
    SELECT xid::text::bigint AS xid, id FROM events
    WHERE xid < pg_snapshot_xmin(pg_current_snapshot())
    ORDER BY xid DESC, id DESC LIMIT 1
    """
)


class CursorOut(BaseModel):
    name: str
    cursor: str | None
    updated_at: datetime | None


class CursorIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cursor: str = Field(max_length=64)


@router.get("/cursors/{name}", response_model=CursorOut)
async def get_cursor(name: str, actor: ActorDep, session: ReadSession) -> CursorOut:
    """Stored SSE position of a service client (null when never stored)."""
    c = await cursors_uc.get_client_cursor(session, actor, name)
    return CursorOut(name=c.name, cursor=c.cursor, updated_at=c.updated_at)


@router.put("/cursors/{name}", response_model=CursorOut)
async def put_cursor(
    name: str, body: CursorIn, actor: ActorDep, session: WriteSession
) -> CursorOut:
    """Store the SSE position of a service client (resume without loss)."""
    c = await cursors_uc.set_client_cursor(session, actor, name, body.cursor)
    return CursorOut(name=c.name, cursor=c.cursor, updated_at=c.updated_at)


@router.get("")
async def stream_events(
    request: Request,
    actor: ActorDep,
    db: DbDep,
    after: str | None = None,
    topics: Annotated[list[str] | None, Query()] = None,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    actor.require(Permission.EVENTS_READ)
    settings = request.app.state.settings
    listener = request.app.state.listener

    raw = last_event_id or after
    if raw in (None, "latest"):
        async with db.session() as session:
            row = (await session.execute(_LATEST)).first()
        cursor = Cursor(row.xid, row.id) if row else Cursor()
    else:
        cursor = Cursor.decode(raw)

    async def generate() -> AsyncIterator[bytes]:
        nonlocal cursor
        waker = listener.subscribe(EVENTS)
        try:
            yield b"retry: 3000\n\n"
            while not await request.is_disconnected():
                async with db.session() as session:
                    batch = await read_events(
                        session, cursor, limit=settings.sse_batch_size, topics=topics
                    )
                for event in batch:
                    cursor = event.cursor
                    data = orjson.dumps(event.to_public()).decode()
                    yield f"id: {cursor.encode()}\nevent: {event.topic}\ndata: {data}\n\n".encode()
                if len(batch) == settings.sse_batch_size:
                    continue
                if not await waker.wait(settings.sse_heartbeat_seconds):
                    yield b": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            listener.unsubscribe(EVENTS, waker)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
