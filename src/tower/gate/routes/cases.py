from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response
from fastapi.routing import APIRoute

from tower.core.domain.cases import CaseKind, CaseStatus
from tower.core.domain.security import Permission
from tower.core.usecases import cases as uc
from tower.core.usecases.drafts import draft_id_of_case
from tower.gate.deps import ActorDep, ReadSession, WriteSession
from tower.gate.schemas import (
    CaseCreate,
    CaseEventOut,
    CaseOut,
    CasePublicOut,
    DiscordThreadIn,
    PublishIn,
    TransitionIn,
)

router = APIRouter(prefix="/v1/cases", tags=["cases"])


@router.get("", response_model=list[CaseOut] | list[CasePublicOut])
async def list_cases(
    actor: ActorDep,
    session: ReadSession,
    status: CaseStatus | None = None,
    kind: CaseKind | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before_id: int | None = None,
) -> list[CaseOut] | list[CasePublicOut]:
    rows = await uc.list_cases(
        session, actor, status=status, kind=kind, limit=limit, before_id=before_id
    )
    if actor.can(Permission.CASE_READ):
        return [CaseOut.of(d) for d in rows]
    return [CasePublicOut.model_validate(CaseOut.of(d).model_dump()) for d in rows]


@router.post("", response_model=CaseOut, status_code=201)
async def open_case(body: CaseCreate, actor: ActorDep, session: WriteSession) -> CaseOut:
    case = await uc.open_case(
        session,
        actor,
        kind=body.kind,
        x_post_id=int(body.x_post_id) if body.x_post_id else None,
        title=body.title,
        priority=body.priority,
        reason=body.reason,
    )
    return CaseOut.of(case)


@router.get("/{public_id}", response_model=CaseOut | CasePublicOut)
async def get_case(
    public_id: uuid.UUID, actor: ActorDep, session: ReadSession
) -> CaseOut | CasePublicOut:
    case = await uc.get_case(session, actor, public_id)
    if actor.can(Permission.CASE_READ):
        return CaseOut.of(case, draft_id=await draft_id_of_case(session, case.id))
    return CasePublicOut.model_validate(CaseOut.of(case).model_dump())


@router.put("/{public_id}/discord-thread", status_code=204)
async def link_discord_thread(
    public_id: uuid.UUID, body: DiscordThreadIn, actor: ActorDep, session: WriteSession
) -> Response:
    """Record the Discord forum post showing the case (intercom)."""
    await uc.link_discord_thread(session, actor, public_id, int(body.thread_id))
    return Response(status_code=204)


@router.get("/{public_id}/history", response_model=list[CaseEventOut])
async def case_history(
    public_id: uuid.UUID, actor: ActorDep, session: ReadSession
) -> list[CaseEventOut]:
    return [CaseEventOut.of(ev) for ev in await uc.history(session, actor, public_id)]


@router.post("/{public_id}/transitions", response_model=CaseOut)
async def transition(
    public_id: uuid.UUID, body: TransitionIn, actor: ActorDep, session: WriteSession
) -> CaseOut:
    case = await uc.transition(
        session,
        actor,
        public_id,
        body.to,
        reason=body.reason,
        expected_version=body.expected_version,
        published_url=body.published_url,
    )
    return CaseOut.of(case)


@router.post("/{public_id}/publish", response_model=CaseOut)
async def mark_published(
    public_id: uuid.UUID, body: PublishIn, actor: ActorDep, session: WriteSession
) -> CaseOut:
    case = await uc.mark_published(
        session, actor, public_id, body.url, expected_version=body.expected_version
    )
    return CaseOut.of(case)


def legacy_router() -> APIRouter:
    """The same endpoints under the old ``/v1/dossiers`` paths, hidden from the schema
    (expand step of the dossier -> case rename; contract: remove)."""
    legacy = APIRouter(include_in_schema=False)
    for route in router.routes:
        if isinstance(route, APIRoute):
            legacy.add_api_route(
                route.path.replace("/v1/cases", "/v1/dossiers", 1),
                route.endpoint,
                methods=sorted(route.methods or ()),
                response_model=route.response_model,
                status_code=route.status_code,
                include_in_schema=False,
            )
    return legacy
