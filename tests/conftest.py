from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

import httpx
import pytest
from sqlalchemy import text

os.environ.setdefault("TOWER_DATABASE_URL", "postgresql://postgres@localhost:5433/tower_test")
os.environ["TOWER_ENV"] = "test"
os.environ.setdefault("TOWER_LOG_FORMAT", "console")

from tower.core.auth.tokens import generate_token, hash_token
from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain.security import Permission
from tower.core.errors import ErrorReporter, set_reporter
from tower.core.events.channels import EVENTS, JOBS
from tower.core.events.listener import PgListener
from tower.core.logging import configure_logging
from tower.core.migrations import downgrade, upgrade
from tower.core.models.auth import ServiceClient, StaffMember

TABLES = [
    "verdict_bands",
    "claim_group_events",
    "content_embeddings",
    "level_overrides",
    "processing_profiles",
    "router_models",
    "citations",
    "context_note_translations",
    "context_notes",
    "reviews",
    "dossier_claim_groups",
    "evidence_passages",
    "verdicts",
    "investigations",
    "claim_group_members",
    "claim_groups",
    "content_claims",
    "claims",
    "sources",
    "reference_facts",
    "source_registry",
    "source_cache",
    "dossier_events",
    "dossiers",
    "events",
    "event_cursors",
    "jobs",
    "service_clients",
    "staff_members",
    "error_reports",
    "llm_calls",
    "llm_routes",
    "llm_models",
    "llm_free_usage",
    "budget_reservations",
    "budget_counters",
    "observations",
    "observation_counters",
    "content_media",
    "media_assets",
    "content_snapshots",
    "content_items",
    "watched_account_events",
    "watched_accounts",
    "platform_accounts",
    "entities",
    "installation_daily",
    "installation_secrets",
    "extension_filter_versions",
]


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "process": "test",
        "job_poll_interval": 0.2,
        "worker_concurrency": 2,
        "job_lease_seconds": 30,
        "shutdown_grace_seconds": 1,
        "auth_cache_ttl": 0,
        "sse_heartbeat_seconds": 0.5,
        "error_flush_interval": 0.2,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture(scope="session")
def settings() -> Settings:
    s = make_settings()
    configure_logging(s)
    return s


@pytest.fixture(scope="session")
def _schema(settings: Settings) -> None:
    downgrade(settings, "base")
    upgrade(settings, "head")


@pytest.fixture(scope="session")
async def db(settings: Settings, _schema: None) -> AsyncIterator[Database]:
    database = Database(settings)
    yield database
    await database.dispose()


@pytest.fixture(autouse=True)
async def _clean(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    if "db" not in request.fixturenames:
        yield
        return
    database: Database = request.getfixturevalue("db")
    async with database.transaction() as session:
        await session.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
    reporter = ErrorReporter(database, process="test")
    set_reporter(reporter)
    yield
    set_reporter(ErrorReporter())


@pytest.fixture
def reporter(db: Database) -> ErrorReporter:
    from tower.core.errors import get_reporter

    return get_reporter()


@pytest.fixture
async def listener(settings: Settings, db: Database) -> AsyncIterator[PgListener]:
    lst = PgListener(settings.asyncpg_dsn, [EVENTS, JOBS])
    await lst.start()
    await lst.connected.wait()
    yield lst
    await lst.stop()


TokenFactory = Callable[..., Coroutine[Any, Any, str]]


@pytest.fixture
def make_token(db: Database) -> TokenFactory:
    async def _make(
        name: str = "intercom", scopes: list[Permission] | None = None, revoked: bool = False
    ) -> str:
        token = generate_token()
        async with db.transaction() as session:
            session.add(
                ServiceClient(
                    name=name,
                    token_hash=hash_token(token),
                    scopes=[p.value for p in (scopes if scopes is not None else list(Permission))],
                )
            )
            if revoked:
                await session.flush()
                await session.execute(
                    text("UPDATE service_clients SET revoked_at = now() WHERE name = :n"),
                    {"n": name},
                )
        return token

    return _make


@pytest.fixture
def add_staff(db: Database) -> Callable[..., Coroutine[Any, Any, None]]:
    async def _add(discord_id: int, roles: list[str], active: bool = True) -> None:
        async with db.transaction() as session:
            session.add(StaffMember(discord_id=discord_id, roles=roles, active=active))

    return _add


@pytest.fixture
async def client(settings: Settings, db: Database) -> AsyncIterator[httpx.AsyncClient]:
    from tower.gate.app import create_app

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gate") as c:
            yield c
