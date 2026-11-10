"""Demo service: full event → job → event loop.

``ping.requested`` (event) → job ``ping.pong`` → ``ping.done`` (event).
Serves as a template for real services and as a production smoke test.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.events.outbox import EventRecord, publish
from tower.core.jobs.queue import enqueue
from tower.core.services.base import JobContext, JobSpec, Service, Subscription

QUEUE = "default"


async def on_ping_requested(session: AsyncSession, events: Sequence[EventRecord]) -> None:
    for event in events:
        await enqueue(
            session,
            QUEUE,
            "ping.pong",
            {"request_event": event.cursor.encode(), **event.payload},
            dedupe_key=f"ping:{event.id}",
        )


async def pong(ctx: JobContext) -> None:
    async with ctx.db.transaction() as session:
        await publish(
            session,
            "ping.done",
            {"job_id": ctx.job.id, "request": ctx.job.payload},
            actor=f"system:{ctx.settings.instance_id}",
        )
    ctx.log.info("ping.pong")


service = Service(
    name="ping",
    description="Round-trip demo and health probe",
    jobs=(JobSpec(kind="ping.pong", handler=pong, queue=QUEUE, timeout=10),),
    subscriptions=(
        Subscription(name="requests", topics=("ping.requested",), handler=on_ping_requested),
    ),
)
