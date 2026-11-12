"""Claim groups: detail, members, manual merge and split."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from tower.core.usecases import group_views, narrative_views
from tower.core.usecases import groups as uc
from tower.gate.deps import ActorDep, ReadSession, WriteSession

router = APIRouter(prefix="/v1/groups", tags=["groups"])


class MemberOut(BaseModel):
    claim_id: str
    text_original: str
    lang: str
    text_fr: str
    representative: bool
    signature: dict[str, Any]
    contents: list[dict[str, Any]]
    added_at: str | None
    removed_at: str | None
    removed_reason: str | None


class GroupOut(BaseModel):
    id: str
    level: str
    state: str
    reach: int
    merged_into: str | None
    reference_fact: dict[str, Any] | None
    band: str | None
    band_side: str | None
    active_verdict: dict[str, Any] | None = Field(
        description="id, status, veracity, expires_at, reuse_rule, reused_from (verdict ids)."
    )
    members: list[MemberOut]
    journal: list[dict[str, Any]] = Field(description="Newest first, at most 50 rows.")
    narrative: dict[str, Any] | None = Field(
        default=None, description="id and state of the narrative of the group, if any."
    )


class MergeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_group_id: uuid.UUID
    reason: str = Field(max_length=1000)


class SplitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_ids: list[uuid.UUID] = Field(max_length=200)
    reason: str = Field(max_length=1000)


async def _group_out(session: ReadSession, actor: ActorDep, group_id: uuid.UUID) -> GroupOut:
    view = await group_views.get_group(session, actor, group_id)
    view["narrative"] = await narrative_views.group_narrative_by_public(session, group_id)
    return GroupOut.model_validate(view)


@router.get("/{group_id}", response_model=GroupOut)
async def get_group(group_id: uuid.UUID, actor: ActorDep, session: ReadSession) -> GroupOut:
    """Group with its reference fact and band, active verdict, active members, journal."""
    return await _group_out(session, actor, group_id)


@router.get("/{group_id}/members", response_model=list[MemberOut])
async def list_members(
    group_id: uuid.UUID,
    actor: ActorDep,
    session: ReadSession,
    include_removed: Annotated[bool, Query()] = False,
) -> list[MemberOut]:
    """Claims of the group (``include_removed``: also those moved by a merge or split)."""
    rows = await group_views.list_members(session, actor, group_id, include_removed=include_removed)
    return [MemberOut.model_validate(r) for r in rows]


@router.post("/{target}/merge", response_model=GroupOut)
async def merge(
    target: uuid.UUID, body: MergeIn, actor: ActorDep, session: WriteSession
) -> GroupOut:
    """Absorb ``source_group_id`` into ``target`` (errors TWR-FCK-021 to 023)."""
    await uc.merge_groups(
        session, actor, target_id=target, source_id=body.source_group_id, reason=body.reason
    )
    return await _group_out(session, actor, target)


@router.post("/{group_id}/split", response_model=GroupOut)
async def split(
    group_id: uuid.UUID, body: SplitIn, actor: ActorDep, session: WriteSession
) -> GroupOut:
    """Move claims into a NEW group without verdict (no verdict is ever propagated)."""
    new = await uc.split_group(
        session, actor, group_id=group_id, claim_ids=body.claim_ids, reason=body.reason
    )
    return await _group_out(session, actor, new.public_id)
