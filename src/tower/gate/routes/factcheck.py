"""Fact-check endpoints: manual requests, draft review queue, public verdict."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Response

from tower.core.usecases import drafts as uc
from tower.gate.deps import ActorDep, ReadSession, WriteSession
from tower.gate.schemas import (
    DraftOut,
    FactcheckRequestIn,
    FactcheckRequestOut,
    PublicVerdictOut,
    ReviewIn,
    ReviewOut,
)

router = APIRouter()

PUBLIC_CACHE_CONTROL = "public, max-age=300"


@router.post(
    "/v1/factcheck/requests",
    response_model=FactcheckRequestOut,
    status_code=202,
    tags=["factcheck"],
)
async def request_factcheck(
    body: FactcheckRequestIn, actor: ActorDep, session: WriteSession
) -> FactcheckRequestOut:
    result = await uc.request_factcheck(
        session, actor, platform=body.platform, external_id=body.external_id, url=body.url
    )
    return FactcheckRequestOut(
        platform=result.platform,
        external_id=result.external_id,
        accepted=True,
        duplicate=result.duplicate,
    )


@router.get("/v1/drafts", response_model=list[DraftOut], tags=["drafts"])
async def list_drafts(
    actor: ActorDep,
    session: ReadSession,
    status: Literal["pending"] = "pending",
    notified: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before_id: int | None = None,
) -> list[DraftOut]:
    views = await uc.list_pending_drafts(
        session, actor, notified=notified, limit=limit, before_id=before_id
    )
    return [DraftOut.of(v) for v in views]


@router.get("/v1/drafts/{draft_id}", response_model=DraftOut, tags=["drafts"])
async def get_draft(draft_id: uuid.UUID, actor: ActorDep, session: ReadSession) -> DraftOut:
    return DraftOut.of(await uc.get_draft(session, actor, draft_id))


@router.post("/v1/drafts/{draft_id}/review", response_model=ReviewOut, tags=["drafts"])
async def review_draft(
    draft_id: uuid.UUID, body: ReviewIn, actor: ActorDep, session: WriteSession
) -> ReviewOut:
    result = await uc.review_draft(
        session,
        actor,
        draft_id,
        decision=body.decision,
        veracity=body.veracity,
        note_text=body.note_text,
        note_lang=body.note_lang,
        labels=body.labels,
        reason=body.reason,
        expected_version=body.expected_version,
        validate_band=body.validate_band,
    )
    return ReviewOut(outcome=result.outcome, draft=DraftOut.of(result.draft))


@router.post("/v1/drafts/{draft_id}/notified", status_code=204, tags=["drafts"])
async def mark_notified(draft_id: uuid.UUID, actor: ActorDep, session: WriteSession) -> Response:
    await uc.mark_draft_notified(session, actor, draft_id)
    return Response(status_code=204)


@router.get(
    "/v1/content/{platform}/{external_id}/verdict",
    response_model=PublicVerdictOut,
    tags=["public"],
)
async def public_verdict(
    platform: str,
    external_id: str,
    response: Response,
    actor: ActorDep,
    session: ReadSession,
    lang: Annotated[str | None, Query(max_length=16)] = None,
) -> PublicVerdictOut:
    verdict = await uc.get_public_verdict(session, actor, platform, external_id, lang)
    response.headers["Cache-Control"] = PUBLIC_CACHE_CONTROL
    return PublicVerdictOut.of(verdict)
