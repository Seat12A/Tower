from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from tower import __version__
from tower.core.auth.resolver import ActorResolver
from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.errors import ErrorReporter, set_reporter
from tower.core.events.channels import EVENTS
from tower.core.events.listener import PgListener
from tower.core.logging import get_logger
from tower.gate.errors import install_error_handlers
from tower.gate.middleware import ObservabilityMiddleware
from tower.gate.ratelimit import TokenBucketLimiter
from tower.gate.routes import (
    dossiers,
    errors,
    events,
    factcheck,
    groups,
    health,
    ingest,
    levels,
    profiles,
    references,
    usage,
    watchlist,
)

log = get_logger(__name__)


def create_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(settings)
        reporter = ErrorReporter(
            db, process=settings.process, flush_interval=settings.error_flush_interval
        )
        set_reporter(reporter)
        reporter_stop = asyncio.Event()
        reporter_task = asyncio.create_task(reporter.run(reporter_stop))
        listener = PgListener(settings.asyncpg_dsn, [EVENTS], ssl=settings.db_ssl)
        await listener.start()
        app.state.settings = settings
        app.state.db = db
        app.state.listener = listener
        app.state.resolver = ActorResolver(db, cache_ttl=settings.auth_cache_ttl)
        log.info("gate.started", port=settings.port)
        try:
            yield
        finally:
            reporter_stop.set()
            await reporter_task
            await listener.stop()
            await db.dispose()
            log.info("gate.stopped")

    app = FastAPI(
        title="12A Tower",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if settings.env != "prod" else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.env != "prod" else None,
    )
    app.state.limiter = TokenBucketLimiter()
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(dossiers.router)
    app.include_router(events.router)
    app.include_router(errors.router)
    app.include_router(ingest.router)
    app.include_router(watchlist.router)
    app.include_router(usage.router)
    app.include_router(factcheck.router)
    app.include_router(profiles.router)
    app.include_router(levels.router)
    app.include_router(groups.router)
    app.include_router(references.router)
    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type", "X-Installation-Token"],
        )
    app.add_middleware(ObservabilityMiddleware)
    return app
