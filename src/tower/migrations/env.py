"""Async Alembic environment. The URL comes from the Tower config, never from an .ini."""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

import tower.core.models  # noqa: F401  (registers the tables)
from tower.core.config import Settings, get_settings
from tower.core.db.base import Base

config = context.config
settings: Settings = config.attributes.get("settings") or get_settings()
target_metadata = Base.metadata


def _configure(connection: object | None = None) -> None:
    context.configure(
        connection=connection,  # type: ignore[arg-type]
        url=None if connection is not None else settings.sqlalchemy_url,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        transaction_per_migration=True,
    )


def run_offline() -> None:
    _configure()
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    connect_args = {"ssl": settings.db_ssl} if settings.db_ssl is not None else {}
    engine = create_async_engine(settings.sqlalchemy_url, connect_args=connect_args)
    async with engine.connect() as conn:
        await conn.run_sync(lambda sync_conn: _do_run(sync_conn))
    await engine.dispose()


def _do_run(connection: object) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_offline()
else:
    asyncio.run(run_online())
