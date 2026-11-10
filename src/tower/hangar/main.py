from __future__ import annotations

import asyncio

from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain import error_codes as ec
from tower.core.errors import ErrorReporter, capture, set_reporter
from tower.core.events.channels import EVENTS, JOBS
from tower.core.events.consumer import run_consumer
from tower.core.events.listener import PgListener
from tower.core.jobs.maintenance import run_maintenance
from tower.core.jobs.runner import JobRunner
from tower.core.logging import get_logger
from tower.core.services.registry import load_services

log = get_logger(__name__)


async def serve(settings: Settings, stop: asyncio.Event) -> None:
    services = load_services(settings.service_names)
    services.validate(lease_seconds=settings.job_lease_seconds)
    queues = settings.queue_names or services.queues

    db = Database(settings)
    reporter = ErrorReporter(
        db, process=settings.process, flush_interval=settings.error_flush_interval
    )
    set_reporter(reporter)
    listener = PgListener(settings.asyncpg_dsn, [JOBS, EVENTS], ssl=settings.db_ssl)
    await listener.start()
    log.info(
        "hangar.started",
        services=[s.name for s in services.services],
        queues=queues,
        consumers=[name for name, _ in services.subscriptions],
    )
    runner = JobRunner(db, listener, services.handlers, queues, settings)
    reporter_stop = asyncio.Event()
    reporter_task = asyncio.create_task(reporter.run(reporter_stop), name="error-reporter")
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(runner.run(stop), name="runner")
            tg.create_task(run_maintenance(db, settings, stop), name="maintenance")
            for name, sub in services.subscriptions:
                tg.create_task(
                    run_consumer(
                        db,
                        listener,
                        name,
                        sub.handler,
                        topics=list(sub.topics),
                        poll_interval=settings.job_poll_interval,
                        stop=stop,
                    ),
                    name=f"consumer:{name}",
                )
    except* Exception as group:
        for exc in group.exceptions:
            capture(ec.INT_SERVICE_CRASHED, exc)
        raise
    finally:
        reporter_stop.set()
        await reporter_task  # final flush of the error buffer
        await listener.stop()
        await db.dispose()
        log.info("hangar.stopped")
