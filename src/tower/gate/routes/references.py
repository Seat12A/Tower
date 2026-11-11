"""Reference facts and value bands (verdict reuse through validated bands)."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain import error_codes as ec
from tower.core.domain.errors import NotFound
from tower.core.domain.factcheck.values import Band, BandSide, ContextNeed, FactOrigin, Veracity
from tower.core.models.factcheck import Verdict
from tower.core.models.factcheck_v1 import ReferenceFact, VerdictBand
from tower.core.usecases import group_views
from tower.core.usecases import references as uc
from tower.gate.deps import ActorDep, ReadSession, WriteSession

router = APIRouter(tags=["references"])


class FactOut(BaseModel):
    id: str
    indicator: str
    label: str
    definition_variant: str | None
    value: str | None
    unit: str
    unit_scale: str | None
    period_start: str
    period_end: str
    territory: str
    source_url: str
    source_name: str
    published_at: str
    next_publication: str | None
    status: str
    origin: str
    created_by: str
    validated_by: str | None
    validated_at: str | None


class BandOut(BaseModel):
    id: str
    fact_id: str
    band: str
    side: str
    label: str
    veracity: str
    context_need: str
    context_types: list[str]
    source_verdict_id: str | None
    status: str
    validated_by: str
    validated_at: str | None
    revoked_by: str | None
    revoked_at: str | None


class FactIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    indicator: str = Field(max_length=100)
    label: str = Field(max_length=300)
    value: Decimal
    unit: str = Field(max_length=32)
    unit_scale: str | None = Field(default=None, max_length=16)
    period_start: date
    period_end: date
    territory: str = Field(max_length=16)
    source_url: str = Field(max_length=2000)
    source_name: str = Field(max_length=300)
    published_at: date
    next_publication: date | None = None
    definition_variant: str | None = Field(default=None, max_length=64)
    subject_qid: str | None = Field(default=None, max_length=32)
    aliases: list[str] = Field(default_factory=list, max_length=50)
    previous_fact_id: uuid.UUID | None = None


class FactFromVerdictIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict_id: uuid.UUID
    value: Decimal
    period_start: date
    period_end: date
    source_url: str = Field(max_length=2000)
    source_name: str = Field(max_length=300)
    published_at: date
    next_publication: date | None = None
    indicator: str | None = Field(default=None, max_length=100)
    label: str | None = Field(default=None, max_length=300)


class ReasonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(max_length=1000)


class BandIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fact_id: uuid.UUID
    band: Band
    side: BandSide
    veracity: Veracity
    context_need: ContextNeed
    context_types: list[str] = Field(default_factory=list, max_length=16)
    source_verdict_id: uuid.UUID | None = None


class BandFromVerdictIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict_id: uuid.UUID


async def _fact_out(session: AsyncSession, fact: ReferenceFact) -> FactOut:
    return FactOut.model_validate(group_views.fact_view(fact))


async def _band_out(session: AsyncSession, row: VerdictBand) -> BandOut:
    fact_public = await session.scalar(
        select(ReferenceFact.public_id).where(ReferenceFact.id == row.reference_fact_id)
    )
    source = (
        await session.scalar(select(Verdict.public_id).where(Verdict.id == row.source_verdict_id))
        if row.source_verdict_id is not None
        else None
    )
    if fact_public is None:  # pragma: no cover - foreign key
        raise NotFound("Reference fact not found", code=ec.FCK_FACT_NOT_FOUND)
    return BandOut.model_validate(group_views.band_view(row, fact_public, source))


@router.post("/v1/reference-facts", response_model=FactOut, status_code=201)
async def create_fact(body: FactIn, actor: ActorDep, session: WriteSession) -> FactOut:
    """New pending fact (it serves only once a reviewer validated it)."""
    fact = await uc.create_reference_fact(
        session, actor, origin=FactOrigin.MANUAL, **body.model_dump()
    )
    return await _fact_out(session, fact)


@router.post("/v1/reference-facts/from-verdict", response_model=FactOut, status_code=201)
async def create_fact_from_verdict(
    body: FactFromVerdictIn, actor: ActorDep, session: WriteSession
) -> FactOut:
    """Fact sheet created from an active verdict of a statistic claim."""
    fact = await uc.create_fact_from_verdict(session, actor, **body.model_dump())
    return await _fact_out(session, fact)


@router.get("/v1/reference-facts", response_model=list[FactOut])
async def list_facts(
    actor: ActorDep,
    session: ReadSession,
    status: Annotated[str | None, Query(max_length=16)] = None,
    indicator: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[FactOut]:
    rows = await group_views.list_facts(
        session, actor, status=status, indicator=indicator, limit=limit
    )
    return [FactOut.model_validate(r) for r in rows]


@router.get("/v1/reference-facts/{fact_id}", response_model=FactOut)
async def get_fact(fact_id: uuid.UUID, actor: ActorDep, session: ReadSession) -> FactOut:
    return FactOut.model_validate(await group_views.get_fact(session, actor, fact_id))


@router.post("/v1/reference-facts/{fact_id}/validate", response_model=FactOut)
async def validate_fact(
    fact_id: uuid.UUID, request: Request, actor: ActorDep, session: WriteSession
) -> FactOut:
    """pending -> validated; the bands of the linked groups are computed."""
    fact = await uc.validate_reference_fact(
        session, actor, fact_id=fact_id, settings=request.app.state.settings
    )
    return await _fact_out(session, fact)


@router.post("/v1/reference-facts/{fact_id}/reject", response_model=FactOut)
async def reject_fact(
    fact_id: uuid.UUID, body: ReasonIn, actor: ActorDep, session: WriteSession
) -> FactOut:
    fact = await uc.reject_reference_fact(session, actor, fact_id=fact_id, reason=body.reason)
    return await _fact_out(session, fact)


@router.get("/v1/reference-facts/{fact_id}/bands", response_model=list[BandOut])
async def list_bands(fact_id: uuid.UUID, actor: ActorDep, session: ReadSession) -> list[BandOut]:
    rows = await group_views.list_bands(session, actor, fact_id)
    return [BandOut.model_validate(r) for r in rows]


@router.post("/v1/verdict-bands", response_model=BandOut, status_code=201)
async def create_band(body: BandIn, actor: ActorDep, session: WriteSession) -> BandOut:
    """Validate the outcome of (fact, band, side) once; new variants inherit it."""
    row = await uc.validate_band(
        session,
        actor,
        fact_id=body.fact_id,
        band=body.band,
        side=body.side,
        veracity=body.veracity,
        context_need=body.context_need,
        context_types=body.context_types,
        source_verdict_id=body.source_verdict_id,
    )
    return await _band_out(session, row)


@router.post("/v1/verdict-bands/from-verdict", response_model=BandOut, status_code=201)
async def create_band_from_verdict(
    body: BandFromVerdictIn, actor: ActorDep, session: WriteSession
) -> BandOut:
    """Band outcome taken from an active verdict (its own veracity and context)."""
    row = await uc.validate_band_from_verdict(session, actor, verdict_id=body.verdict_id)
    return await _band_out(session, row)


@router.post("/v1/verdict-bands/{band_id}/revoke", response_model=BandOut)
async def revoke_band(
    band_id: uuid.UUID, body: ReasonIn, actor: ActorDep, session: WriteSession
) -> BandOut:
    row = await uc.revoke_band(session, actor, band_id=band_id, reason=body.reason)
    return await _band_out(session, row)
