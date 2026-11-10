from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from tower.core.domain.dossiers import DossierKind, DossierStatus
from tower.core.domain.security import Permission
from tower.core.usecases import dossiers as uc
from tower.gate.deps import ActorDep, ReadSession, WriteSession
from tower.gate.schemas import (
    DossierCreate,
    DossierEventOut,
    DossierOut,
    DossierPublicOut,
    PublishIn,
    TransitionIn,
)

router = APIRouter(prefix="/v1/dossiers", tags=["dossiers"])


@router.get("", response_model=list[DossierOut] | list[DossierPublicOut])
async def list_dossiers(
    actor: ActorDep,
    session: ReadSession,
    status: DossierStatus | None = None,
    kind: DossierKind | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before_id: int | None = None,
) -> list[DossierOut] | list[DossierPublicOut]:
    rows = await uc.list_dossiers(
        session, actor, status=status, kind=kind, limit=limit, before_id=before_id
    )
    if actor.can(Permission.DOSSIER_READ):
        return [DossierOut.of(d) for d in rows]
    return [DossierPublicOut.model_validate(DossierOut.of(d).model_dump()) for d in rows]


@router.post("", response_model=DossierOut, status_code=201)
async def open_dossier(body: DossierCreate, actor: ActorDep, session: WriteSession) -> DossierOut:
    dossier = await uc.open_dossier(
        session,
        actor,
        kind=body.kind,
        x_post_id=int(body.x_post_id) if body.x_post_id else None,
        title=body.title,
        priority=body.priority,
        reason=body.reason,
    )
    return DossierOut.of(dossier)


@router.get("/{public_id}", response_model=DossierOut | DossierPublicOut)
async def get_dossier(
    public_id: uuid.UUID, actor: ActorDep, session: ReadSession
) -> DossierOut | DossierPublicOut:
    out = DossierOut.of(await uc.get_dossier(session, actor, public_id))
    if actor.can(Permission.DOSSIER_READ):
        return out
    return DossierPublicOut.model_validate(out.model_dump())


@router.get("/{public_id}/history", response_model=list[DossierEventOut])
async def dossier_history(
    public_id: uuid.UUID, actor: ActorDep, session: ReadSession
) -> list[DossierEventOut]:
    return [DossierEventOut.of(ev) for ev in await uc.history(session, actor, public_id)]


@router.post("/{public_id}/transitions", response_model=DossierOut)
async def transition(
    public_id: uuid.UUID, body: TransitionIn, actor: ActorDep, session: WriteSession
) -> DossierOut:
    dossier = await uc.transition(
        session,
        actor,
        public_id,
        body.to,
        reason=body.reason,
        expected_version=body.expected_version,
        published_url=body.published_url,
    )
    return DossierOut.of(dossier)


@router.post("/{public_id}/publish", response_model=DossierOut)
async def mark_published(
    public_id: uuid.UUID, body: PublishIn, actor: ActorDep, session: WriteSession
) -> DossierOut:
    dossier = await uc.mark_published(
        session, actor, public_id, body.url, expected_version=body.expected_version
    )
    return DossierOut.of(dossier)
