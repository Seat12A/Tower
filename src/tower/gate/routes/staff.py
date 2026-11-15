from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Path, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from tower.core.usecases import staff as uc
from tower.gate.deps import ActorDep, ReadSession, WriteSession

router = APIRouter(prefix="/v1/staff", tags=["staff"])

DiscordId = Annotated[int, Path(ge=1, description="Discord id of the member")]


class StaffOut(BaseModel):
    discord_id: str
    display_name: str | None
    active: bool
    is_admin: bool
    roles: list[str]
    permissions: list[str]
    """Explicit individual permissions."""
    effective_permissions: list[str]
    """What the member holds (roles + individual; everything for an administrator)."""
    created_at: datetime | None
    updated_at: datetime | None

    @classmethod
    def of(cls, m: uc.Member) -> StaffOut:
        return cls(
            discord_id=str(m.discord_id),
            display_name=m.display_name,
            active=m.active,
            is_admin=m.is_admin,
            roles=list(m.roles),
            permissions=list(m.permissions),
            effective_permissions=list(m.effective_permissions),
            created_at=m.created_at,
            updated_at=m.updated_at,
        )


class StaffMeOut(StaffOut):
    actor_permissions: list[str]
    """Permissions of the acting member through this service (already ∩ its scopes)."""


class StaffList(BaseModel):
    items: list[StaffOut]


class RankBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(default=None, max_length=100)


class PermissionsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    permissions: list[str] = Field(max_length=64)


def _admin_ids(request: Request) -> frozenset[int]:
    return frozenset(request.app.state.settings.admin_discord_ids)


def _forget(request: Request, background: BackgroundTasks, discord_id: int) -> None:
    """Drop the cached actor now and again once the transaction has committed (a
    background task runs after the session dependency has exited), so a concurrent
    request cannot re-cache the old permissions."""
    resolver = request.app.state.resolver
    resolver.forget(discord_id)
    background.add_task(resolver.forget, discord_id)


@router.get("/me", response_model=StaffMeOut)
async def get_me(request: Request, actor: ActorDep, session: ReadSession) -> StaffMeOut:
    member = await uc.me(session, actor, _admin_ids(request))
    return StaffMeOut(
        **StaffOut.of(member).model_dump(),
        actor_permissions=sorted(p.value for p in actor.permissions),
    )


@router.get("", response_model=StaffList)
async def list_staff(
    request: Request, actor: ActorDep, session: ReadSession, include_inactive: bool = False
) -> StaffList:
    members = await uc.list_members(
        session, actor, _admin_ids(request), include_inactive=include_inactive
    )
    return StaffList(items=[StaffOut.of(m) for m in members])


@router.get("/{discord_id}", response_model=StaffOut)
async def get_staff(
    discord_id: DiscordId, request: Request, actor: ActorDep, session: ReadSession
) -> StaffOut:
    return StaffOut.of(await uc.get_member(session, actor, discord_id, _admin_ids(request)))


@router.put("/{discord_id}", response_model=StaffOut)
async def rank_staff(
    background: BackgroundTasks,
    discord_id: DiscordId,
    request: Request,
    actor: ActorDep,
    session: WriteSession,
    body: RankBody | None = None,
) -> StaffOut:
    member = await uc.rank(
        session, actor, discord_id, body.display_name if body else None, _admin_ids(request)
    )
    _forget(request, background, discord_id)
    return StaffOut.of(member)


@router.delete("/{discord_id}", status_code=204)
async def unrank_staff(
    background: BackgroundTasks,
    discord_id: DiscordId,
    request: Request,
    actor: ActorDep,
    session: WriteSession,
) -> Response:
    await uc.unrank(session, actor, discord_id, _admin_ids(request))
    _forget(request, background, discord_id)
    return Response(status_code=204)


@router.put("/{discord_id}/permissions", response_model=StaffOut)
async def set_staff_permissions(
    background: BackgroundTasks,
    discord_id: DiscordId,
    body: PermissionsBody,
    request: Request,
    actor: ActorDep,
    session: WriteSession,
) -> StaffOut:
    member = await uc.set_permissions(
        session, actor, discord_id, body.permissions, _admin_ids(request)
    )
    _forget(request, background, discord_id)
    return StaffOut.of(member)
