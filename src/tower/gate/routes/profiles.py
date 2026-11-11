"""Processing profiles: per-level settings, versioned, with safety bounds."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from tower.core.domain.factcheck.profiles import profile_to_json
from tower.core.domain.security import Permission
from tower.core.models.factcheck_v1 import ProcessingProfile
from tower.core.usecases import profiles as uc
from tower.gate.deps import ActorDep, ReadSession, WriteSession

router = APIRouter(prefix="/v1/profiles", tags=["profiles"])


class ProfileVersionOut(BaseModel):
    version: int
    active: bool
    author: str
    reason: str
    created_at: datetime | None
    activated_at: datetime | None
    based_on_version: int | None

    @classmethod
    def of(cls, row: ProcessingProfile) -> ProfileVersionOut:
        return cls(
            version=row.version,
            active=row.active,
            author=row.author,
            reason=row.reason,
            created_at=row.created_at,
            activated_at=row.activated_at,
            based_on_version=row.based_on_version,
        )


class ProfileOut(BaseModel):
    version: int
    settings: dict[str, Any]


class ProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    settings: dict[str, Any] | None = Field(
        default=None, description="Full settings of the new version (alternative to changes)."
    )
    changes: dict[str, Any] | None = Field(
        default=None,
        description="Flat dotted paths applied to the active profile, "
        'e.g. {"levels.N1.e1_threshold": 0.2}.',
    )
    reason: str = Field(max_length=1000)
    activate: bool = False


class ReasonBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(max_length=1000)


@router.get("", response_model=list[ProfileVersionOut])
async def list_versions(
    request: Request, actor: ActorDep, session: ReadSession
) -> list[ProfileVersionOut]:
    """Stored versions, newest first, then version 0 (the default built from the settings)."""
    rows = await uc.list_profiles(session, actor)
    out = [ProfileVersionOut.of(r) for r in rows]
    out.append(
        ProfileVersionOut(
            version=0,
            active=not any(r.active for r in rows),
            author="system",
            reason="Default profile built from the settings",
            created_at=None,
            activated_at=None,
            based_on_version=None,
        )
    )
    return out


@router.get("/active", response_model=ProfileOut)
async def active(request: Request, actor: ActorDep, session: ReadSession) -> ProfileOut:
    """The profile in force (version 0 = default) with its full settings."""
    profile = await uc.current_profile(session, actor, app_settings=request.app.state.settings)
    return ProfileOut(version=profile.version, settings=profile_to_json(profile))


@router.post("", response_model=ProfileVersionOut, status_code=201)
async def create_version(
    body: ProfileCreate, request: Request, actor: ActorDep, session: WriteSession
) -> ProfileVersionOut:
    """New version from full settings or flat changes; optionally activated at once."""
    actor.require(Permission.PROFILES_MANAGE)
    row = await uc.create_profile_version(
        session,
        actor,
        settings=body.settings,
        changes=body.changes,
        reason=body.reason,
        activate=body.activate,
        app_settings=request.app.state.settings,
    )
    return ProfileVersionOut.of(row)


@router.post("/rollback", response_model=ProfileOut)
async def rollback(
    body: ReasonBody, request: Request, actor: ActorDep, session: WriteSession
) -> ProfileOut:
    """Re-activate the version that was active before the current one."""
    profile = await uc.rollback_profile(
        session, actor, reason=body.reason, app_settings=request.app.state.settings
    )
    return ProfileOut(version=profile.version, settings=profile_to_json(profile))


@router.post("/{version}/activate", response_model=ProfileOut)
async def activate(
    version: int, body: ReasonBody, request: Request, actor: ActorDep, session: WriteSession
) -> ProfileOut:
    """Activate a stored version (0 = back to the default profile)."""
    profile = await uc.activate_profile(
        session,
        actor,
        version=version,
        reason=body.reason,
        app_settings=request.app.state.settings,
    )
    return ProfileOut(version=profile.version, settings=profile_to_json(profile))
