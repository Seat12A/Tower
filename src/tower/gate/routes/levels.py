"""Manual level overrides of a content, a group or an account."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from tower.core.domain.factcheck.values import OverrideTarget
from tower.core.models.factcheck_v1 import LevelOverride
from tower.core.usecases import levels as uc
from tower.gate.deps import ActorDep, ReadSession, WriteSession

router = APIRouter(prefix="/v1/level-overrides", tags=["levels"])


class OverrideOut(BaseModel):
    id: uuid.UUID
    target_type: str
    target: str
    level: str
    expires_at: datetime
    author: str
    reason: str
    revoked_at: datetime | None
    revoked_by: str | None
    created_at: datetime
    applied: bool | None = Field(
        default=None,
        description="Creation only: every targeted group now has the forced level "
        "(false for an account, a content without group, or a lowering deferred by a "
        "running investigation).",
    )

    @classmethod
    def of(cls, o: LevelOverride, target: str, applied: bool | None = None) -> OverrideOut:
        return cls(
            id=o.public_id,
            target_type=o.target_type,
            target=target,
            level=o.level,
            expires_at=o.expires_at,
            author=o.author,
            reason=o.reason,
            revoked_at=o.revoked_at,
            revoked_by=o.revoked_by,
            created_at=o.created_at,
            applied=applied,
        )


class OverrideCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_type: OverrideTarget
    target: str = Field(
        min_length=1,
        max_length=300,
        description="Content: public id or '<platform>:<external id>'. Group: public id. "
        "Account: '<platform>:@<handle>' or '<platform>:<external id>'.",
    )
    level: str = Field(max_length=16)
    hours: float = Field(description="Duration, 0 < hours <= 720.")
    reason: str = Field(max_length=1000)


class RevokeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(max_length=1000)


@router.post("", response_model=OverrideOut, status_code=201)
async def create_override(
    body: OverrideCreate, actor: ActorDep, session: WriteSession
) -> OverrideOut:
    """Force a level for a limited time; applied now to the groups it targets."""
    result = await uc.create_override(
        session,
        actor,
        target_type=body.target_type,
        target_ref=body.target,
        level=body.level,
        hours=body.hours,
        reason=body.reason,
    )
    return OverrideOut.of(result.override, result.target, result.applied)


@router.get("", response_model=list[OverrideOut])
async def list_overrides(
    actor: ActorDep,
    session: ReadSession,
    active: bool = True,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[OverrideOut]:
    """Overrides, newest first (``active``: not revoked and not expired)."""
    rows = await uc.list_overrides(session, actor, active=active, limit=limit)
    labels = await uc.target_labels(session, rows)
    return [OverrideOut.of(o, labels.get(o.id, "")) for o in rows]


@router.post("/{override_id}/revoke", response_model=OverrideOut)
async def revoke_override(
    override_id: uuid.UUID, body: RevokeBody, actor: ActorDep, session: WriteSession
) -> OverrideOut:
    """End an override early (group levels are left as they are)."""
    row = await uc.revoke_override(session, actor, override_id=override_id, reason=body.reason)
    labels = await uc.target_labels(session, [row])
    return OverrideOut.of(row, labels.get(row.id, ""))
