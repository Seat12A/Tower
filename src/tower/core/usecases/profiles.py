"""Processing profiles: versioned per-level settings (docs/factcheck/niveaux.md).

Creating a version never changes behaviour; activating one does (one active row, or none
for the default profile built from the settings, version 0). The activation is the audit:
the row columns plus the ``processing_profile.activated`` event.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.config import Settings, get_settings
from tower.core.domain import error_codes as ec
from tower.core.domain.errors import InvalidInput, NotFound
from tower.core.domain.factcheck.profiles import (
    LEVELS,
    Profile,
    bounds_violations,
    diff_profiles,
    profile_from_json,
    profile_to_json,
)
from tower.core.domain.llm import month_period
from tower.core.domain.security import Actor, Permission
from tower.core.events.outbox import publish
from tower.core.factcheck import names
from tower.core.factcheck.profiles import default_profile, invalidate_profile_cache
from tower.core.logging import get_logger
from tower.core.models.factcheck_v1 import ProcessingProfile
from tower.core.models.llm import BudgetCounter

__all__ = [
    "activate_profile",
    "create_profile_version",
    "current_profile",
    "list_profiles",
    "rollback_profile",
]

log = get_logger(__name__)

AGGREGATE = "processing_profile"


def _reason(value: str) -> str:
    reason = (value or "").strip()
    if not reason:
        raise InvalidInput("A reason is required", code=ec.FCK_PROFILE_OUT_OF_BOUNDS)
    return reason


async def _active_row(session: AsyncSession) -> ProcessingProfile | None:
    return await session.scalar(select(ProcessingProfile).where(ProcessingProfile.active))


async def current_profile(
    session: AsyncSession, actor: Actor, *, app_settings: Settings | None = None
) -> Profile:
    """The profile in force, read from the database (no per-process cache)."""
    actor.require(Permission.PROFILES_READ)
    return await _current(session, app_settings or get_settings())


async def _current(session: AsyncSession, app_settings: Settings) -> Profile:
    row = await _active_row(session)
    if row is None:
        return default_profile(app_settings)
    return profile_from_json(row.version, row.settings)


async def list_profiles(session: AsyncSession, actor: Actor) -> list[ProcessingProfile]:
    """Stored versions, newest first (the default profile has no row)."""
    actor.require(Permission.PROFILES_READ)
    rows = await session.scalars(
        select(ProcessingProfile).order_by(ProcessingProfile.version.desc())
    )
    return list(rows)


def _parse(version: int, data: Mapping[str, Any]) -> Profile:
    try:
        profile = profile_from_json(version, data)
    except ValueError as exc:
        raise InvalidInput(
            "Invalid profile settings",
            code=ec.FCK_PROFILE_OUT_OF_BOUNDS,
            violations=[str(exc)],
        ) from None
    violations = bounds_violations(profile)
    if violations:
        raise InvalidInput(
            "Profile outside the safety bounds",
            code=ec.FCK_PROFILE_OUT_OF_BOUNDS,
            violations=violations,
        )
    return profile


def _apply_changes(base: Mapping[str, Any], changes: Mapping[str, Any]) -> dict[str, Any]:
    """Set flat dotted paths on a copy of ``base``; only existing leaves can be changed."""
    out = copy.deepcopy(dict(base))
    for path, value in changes.items():
        parts = path.split(".")
        node: Any = out
        for part in parts[:-1]:
            node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict) or parts[-1] not in node or isinstance(node[parts[-1]], dict):
            raise InvalidInput(
                "Unknown profile setting", code=ec.FCK_PROFILE_OUT_OF_BOUNDS, path=str(path)[:120]
            )
        node[parts[-1]] = value
    return out


async def create_profile_version(
    session: AsyncSession,
    actor: Actor,
    *,
    settings: Mapping[str, Any] | None = None,
    changes: Mapping[str, Any] | None = None,
    reason: str,
    activate: bool = False,
    app_settings: Settings | None = None,
) -> ProcessingProfile:
    """New version from full ``settings`` or from flat ``changes`` on the active profile."""
    actor.require(Permission.PROFILES_MANAGE)
    reason = _reason(reason)
    if (settings is None) == (changes is None):
        raise InvalidInput(
            "Give either full settings or changes", code=ec.FCK_PROFILE_OUT_OF_BOUNDS
        )
    base = await _current(session, app_settings or get_settings())
    data = (
        dict(settings)
        if settings is not None
        else _apply_changes(profile_to_json(base), changes or {})
    )
    # The lock serializes concurrent creations (the version is max + 1).
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext('processing_profiles'))"))
    latest = await session.scalar(select(func.max(ProcessingProfile.version)))
    version = (latest or 0) + 1
    parsed = _parse(version, data)
    row = ProcessingProfile(
        version=version,
        settings=profile_to_json(parsed),
        active=False,
        based_on_version=base.version,
        author=actor.label,
        reason=reason,
    )
    session.add(row)
    await session.flush()
    log.info("profile.created", version=version, based_on=base.version)
    if activate:
        await activate_profile(
            session, actor, version=version, reason=reason, app_settings=app_settings
        )
        await session.refresh(row)
    return row


async def _set_level_caps(session: AsyncSession, profile: Profile, now: datetime) -> None:
    start, end = month_period(now)
    for level in LEVELS:
        stmt = pg_insert(BudgetCounter).values(
            scope=names.level_scope(level),
            period_start=start,
            period_end=end,
            cap_usd_micros=round(profile.level(level).monthly_budget_usd * 1_000_000),
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["scope", "period_start"],
                set_={"cap_usd_micros": stmt.excluded.cap_usd_micros, "updated_at": text("now()")},
            )
        )


async def activate_profile(
    session: AsyncSession,
    actor: Actor,
    *,
    version: int,
    reason: str,
    app_settings: Settings | None = None,
    now: datetime | None = None,
) -> Profile:
    """Make ``version`` the active profile (0 = back to the default profile)."""
    actor.require(Permission.PROFILES_MANAGE)
    reason = _reason(reason)
    cfg = app_settings or get_settings()
    now = now or datetime.now(UTC)
    previous = await _current(session, cfg)
    target_row: ProcessingProfile | None = None
    if version == 0:
        new = default_profile(cfg)
    else:
        target_row = await session.scalar(
            select(ProcessingProfile).where(ProcessingProfile.version == version)
        )
        if target_row is None:
            raise NotFound(
                "Processing profile version not found",
                code=ec.FCK_PROFILE_NOT_FOUND,
                version=version,
            )
        new = _parse(version, target_row.settings)
    if previous.version == new.version:
        return new
    active = await _active_row(session)
    if active is not None:
        active.active = False
        await session.flush()  # the unique partial index allows a single active row
    if target_row is not None:
        target_row.active = True
        target_row.activated_at = now
        target_row.activated_by = actor.label
    await session.flush()
    await _set_level_caps(session, new, now)
    # The cache is per process and short-lived (30 s): other replicas catch up by TTL.
    invalidate_profile_cache()
    changes = {path: [old, new_] for path, (old, new_) in diff_profiles(previous, new).items()}
    await publish(
        session,
        names.PROCESSING_PROFILE_ACTIVATED,
        {
            "version": new.version,
            "previous_version": previous.version,
            "actor": actor.label,
            "reason": reason,
            "changes": changes,
        },
        aggregate_type=AGGREGATE,
        aggregate_id=new.version,
        actor=actor.label,
    )
    log.info("profile.activated", version=new.version, previous=previous.version)
    return new


async def rollback_profile(
    session: AsyncSession,
    actor: Actor,
    *,
    reason: str,
    app_settings: Settings | None = None,
) -> Profile:
    """Activate the version that was active before the current one (none: the default)."""
    actor.require(Permission.PROFILES_MANAGE)
    target = await session.scalar(
        select(ProcessingProfile.version)
        .where(ProcessingProfile.activated_at.is_not(None), ProcessingProfile.active.is_(False))
        .order_by(ProcessingProfile.activated_at.desc(), ProcessingProfile.version.desc())
        .limit(1)
    )
    return await activate_profile(
        session, actor, version=target or 0, reason=reason, app_settings=app_settings
    )
