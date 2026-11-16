from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.db.database import Database
from tower.core.domain import error_codes as ec
from tower.core.domain.errors import Unauthenticated
from tower.core.domain.security import Actor


def get_db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


async def read_session(db: Annotated[Database, Depends(get_db)]) -> AsyncIterator[AsyncSession]:
    async with db.session() as session:
        yield session


async def write_session(db: Annotated[Database, Depends(get_db)]) -> AsyncIterator[AsyncSession]:
    async with db.transaction() as session:
        yield session


async def current_actor(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_acting_discord_id: Annotated[str | None, Header()] = None,
    x_organization_token: Annotated[str | None, Header()] = None,
) -> Actor:
    if x_organization_token is not None:
        if authorization or x_acting_discord_id:
            raise Unauthenticated(
                "X-Organization-Token cannot be combined with another credential",
                code=ec.BDG_ORG_TOKEN_MIXED,
            )
        org: Actor = await request.app.state.resolver.resolve_organization(
            x_organization_token.strip()
        )
        return org
    token = None
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            token = value.strip()
    actor: Actor = await request.app.state.resolver.resolve(token, x_acting_discord_id)
    return actor


ActorDep = Annotated[Actor, Depends(current_actor)]
ReadSession = Annotated[AsyncSession, Depends(read_session)]
WriteSession = Annotated[AsyncSession, Depends(write_session)]
DbDep = Annotated[Database, Depends(get_db)]
