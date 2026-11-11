from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from tower.core.domain.platforms import PublicRole
from tower.core.models.ingestion import PlatformAccount, WatchedAccount
from tower.core.usecases import watchlist as uc
from tower.gate.deps import ActorDep, ReadSession, WriteSession

router = APIRouter(prefix="/v1/watched-accounts", tags=["watchlist"])


class WatchedOut(BaseModel):
    public_id: uuid.UUID
    platform: str
    external_id: str | None
    handle: str | None
    category: str
    priority: int
    active: bool
    note: str | None
    version: int

    @classmethod
    def of(cls, w: WatchedAccount, a: PlatformAccount) -> WatchedOut:
        return cls.model_validate(uc.watched_account_payload(w, a))


class WatchedCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: str = Field(max_length=32)
    external_id: str | None = Field(default=None, max_length=512)
    handle: str | None = Field(default=None, max_length=512)
    category: PublicRole
    priority: int = Field(default=0, ge=-100, le=100)
    note: str | None = Field(default=None, max_length=2000)


class WatchedUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int
    category: PublicRole | None = None
    priority: int | None = Field(default=None, ge=-100, le=100)
    active: bool | None = None
    note: str | None = Field(default=None, max_length=2000)
    handle: str | None = Field(default=None, max_length=512)


@router.get("", response_model=list[WatchedOut])
async def list_watched(
    actor: ActorDep,
    session: ReadSession,
    active: bool | None = None,
    platform: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[WatchedOut]:
    rows = await uc.list_watched_accounts(
        session, actor, active=active, platform=platform, limit=limit, offset=offset
    )
    return [WatchedOut.of(w, a) for w, a in rows]


@router.post("", response_model=WatchedOut, status_code=201)
async def add_watched(body: WatchedCreate, actor: ActorDep, session: WriteSession) -> WatchedOut:
    w, a = await uc.add_watched_account(
        session,
        actor,
        platform=body.platform,
        external_id=body.external_id,
        handle=body.handle,
        category=body.category.value,
        priority=body.priority,
        note=body.note,
    )
    return WatchedOut.of(w, a)


@router.patch("/{public_id}", response_model=WatchedOut)
async def update_watched(
    public_id: uuid.UUID, body: WatchedUpdate, actor: ActorDep, session: WriteSession
) -> WatchedOut:
    kwargs: dict[str, Any] = {}
    if body.category is not None:
        kwargs["category"] = body.category.value
    w, a = await uc.update_watched_account(
        session,
        actor,
        public_id,
        version=body.version,
        priority=body.priority,
        active=body.active,
        note=body.note,
        handle=body.handle,
        **kwargs,
    )
    return WatchedOut.of(w, a)
