"""Narratives: list, detail, manual merge and split, title validation."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from tower.core.usecases import narrative_views
from tower.core.usecases import narratives as uc
from tower.gate.deps import ActorDep, ReadSession, WriteSession

router = APIRouter(prefix="/v1/narratives", tags=["narratives"])

NarrativeStateParam = Literal["active", "watch", "surge", "alert", "closed"]


class NarrativeStatsOut(BaseModel):
    window_end: str
    share: float
    accounts: int
    platforms: int


class NarrativeSummaryOut(BaseModel):
    id: str
    title: str | None
    title_status: str
    state: str
    theme: str | None
    groups: int
    stats_1h: NarrativeStatsOut | None
    opened_at: str | None
    last_activity_at: str | None


class NarrativeOut(BaseModel):
    id: str
    title: str | None
    title_status: str
    state: str
    theme: str | None
    groups_count: int
    stats_1h: NarrativeStatsOut | None
    opened_at: str | None
    last_activity_at: str | None
    closed_at: str | None
    medoids: list[str] = Field(description="Public ids of the medoid groups.")
    groups: list[dict[str, Any]] = Field(
        description="id, level, state, verdict (id, status, veracity), light_signal, reach."
    )
    stats: dict[str, list[dict[str, Any]]] = Field(
        description="Windows 5m, 1h, 6h: the last 12 points each, oldest first."
    )
    journal: list[dict[str, Any]] = Field(description="Newest first, at most 50 rows.")
    representative_group: str | None
    previous_narrative: str | None
    merged_into: str | None
    split_suggested: bool
    novelty: list[dict[str, Any]] = Field(
        description="Fingerprints that took (forced into extraction), at most 10."
    )


class NarrativeMergeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: uuid.UUID
    reason: str = Field(min_length=1, max_length=1000)


class NarrativeSplitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_ids: list[uuid.UUID] = Field(max_length=200)
    reason: str = Field(min_length=1, max_length=1000)


class NarrativeTitleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=300)
    """Corrected title; omitted: validates the draft title as it is."""


@router.get("", response_model=list[NarrativeSummaryOut])
async def list_narratives(
    actor: ActorDep,
    session: ReadSession,
    state: NarrativeStateParam | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[NarrativeSummaryOut]:
    """Narratives by last activity (without ``state``: every open narrative)."""
    rows = await narrative_views.list_narratives(session, actor, state=state, limit=limit)
    return [NarrativeSummaryOut.model_validate(r) for r in rows]


@router.get("/{narrative_id}", response_model=NarrativeOut)
async def get_narrative(
    narrative_id: uuid.UUID, actor: ActorDep, session: ReadSession
) -> NarrativeOut:
    """Narrative detail (error TWR-FCK-043 when unknown)."""
    return NarrativeOut.model_validate(
        await narrative_views.narrative_detail(session, actor, narrative_id)
    )


@router.post("/{target}/merge", response_model=NarrativeOut)
async def merge(
    target: uuid.UUID, body: NarrativeMergeIn, actor: ActorDep, session: WriteSession
) -> NarrativeOut:
    """Absorb ``source_id`` into ``target`` (errors TWR-FCK-043 and TWR-FCK-044)."""
    await uc.merge_narratives(
        session, actor, target_id=target, source_id=body.source_id, reason=body.reason
    )
    return NarrativeOut.model_validate(
        await narrative_views.narrative_detail(session, actor, target)
    )


@router.post("/{narrative_id}/split", response_model=NarrativeOut)
async def split(
    narrative_id: uuid.UUID, body: NarrativeSplitIn, actor: ActorDep, session: WriteSession
) -> NarrativeOut:
    """Move ``group_ids`` into a NEW narrative; the answer is the new narrative."""
    created = await uc.split_narrative(
        session, actor, narrative_id=narrative_id, group_ids=body.group_ids, reason=body.reason
    )
    return NarrativeOut.model_validate(
        await narrative_views.narrative_detail(session, actor, created.public_id)
    )


@router.post("/{narrative_id}/title", response_model=NarrativeOut)
async def set_title(
    narrative_id: uuid.UUID, body: NarrativeTitleIn, actor: ActorDep, session: WriteSession
) -> NarrativeOut:
    """Validate the draft title, or set a corrected one (``dossiers:review``)."""
    await uc.validate_title(session, actor, narrative_id, title=body.title)
    return NarrativeOut.model_validate(
        await narrative_views.narrative_detail(session, actor, narrative_id)
    )
