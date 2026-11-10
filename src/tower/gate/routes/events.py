"""Flux d'événements en Server-Sent Events, pour le bot et les clients internes.

- Reprise : en-tête ``Last-Event-ID`` (envoyé automatiquement par EventSource) ou
  paramètre ``after``. Sans curseur, le flux part de la fin (``after=latest``) ou du
  début (``after=0``).
- Réveil par la connexion LISTEN partagée du processus : pas de connexion SQL
  maintenue par client, une session courte par lot.
- Heartbeat régulier (commentaire SSE) pour garder la connexion ouverte à travers le
  proxy Railway.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated

import orjson
from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text

from tower.core.domain.security import Permission
from tower.core.events.channels import EVENTS
from tower.core.events.outbox import Cursor, read_events
from tower.gate.deps import ActorDep, DbDep

router = APIRouter(prefix="/v1/events", tags=["événements"])

_LATEST = text(
    """
    SELECT xid::text::bigint AS xid, id FROM events
    WHERE xid < pg_snapshot_xmin(pg_current_snapshot())
    ORDER BY xid DESC, id DESC LIMIT 1
    """
)


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
