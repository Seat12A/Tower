"""Engine, sessions and transactions.

One ``Database`` object per process. The pool is intentionally small: each
Railway replica opens at most ``pool_size + max_overflow`` connections, plus one
dedicated connection for ``LISTEN``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import orjson
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tower.core.config import Settings


def _json_serializer(obj: Any) -> str:
    return orjson.dumps(obj).decode()


class Database:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        connect_args: dict[str, Any] = {
            "server_settings": {
                "application_name": f"tower:{settings.instance_id}"[:63],
                "statement_timeout": str(settings.db_statement_timeout_ms),
            },
        }
        if settings.db_ssl is not None:
            connect_args["ssl"] = settings.db_ssl
        self.engine: AsyncEngine = create_async_engine(
            settings.sqlalchemy_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout,
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args=connect_args,
            json_serializer=_json_serializer,
            json_deserializer=orjson.loads,
        )
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Session without an explicit transaction (reads)."""
        async with self.sessionmaker() as session:
            yield session

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """Unit of work: commit on exit, rollback on exception."""
        async with self.sessionmaker() as session, session.begin():
            yield session

    async def ping(self) -> None:
        async with self.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def dispose(self) -> None:
        await self.engine.dispose()
