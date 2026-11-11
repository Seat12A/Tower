"""Active processing profile: the active ``processing_profiles`` row, or the default
profile built from the settings (version 0). Cached per process for a short time
(bounded: one entry); ``invalidate_profile_cache`` is called on activation."""

from __future__ import annotations

import json
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.config import Settings
from tower.core.domain.factcheck.profiles import LEVELS, LevelSettings, Profile, profile_from_json
from tower.core.models.factcheck_v1 import ProcessingProfile

__all__ = ["active_profile", "default_profile", "invalidate_profile_cache"]

CACHE_TTL_S = 30.0
_cache: tuple[float, Profile] | None = None


def default_profile(settings: Settings) -> Profile:
    thresholds: dict[str, float] = json.loads(settings.fc_e1_thresholds or "{}")
    budgets: dict[str, float] = json.loads(settings.fc_level_budgets_usd or "{}")
    caps = {
        "N0": 0.0,
        "N1": 0.0,
        "N2": settings.fc_cap_standard_usd,
        "N3": settings.fc_cap_reinforced_usd,
        "N4": settings.fc_cap_n4_usd,
    }
    levels = {}
    for name in LEVELS:
        budget = float(budgets.get(name, 0.0))
        levels[name] = LevelSettings(
            e1_threshold=1.0 if name == "N0" else float(thresholds.get(name, 0.0)),
            direct_reuse=name != "N4",
            unit_cap_usd=min(caps[name], budget),
            monthly_budget_usd=budget,
        )
    return Profile(
        version=0,
        levels=levels,
        match_same_threshold=settings.fc_match_same_threshold,
        match_grey_threshold=settings.fc_match_grey_threshold,
        draft_daily_quota=settings.fc_draft_daily_quota,
        e1_sample_rate=settings.fc_e1_sample_rate,
    )


async def active_profile(session: AsyncSession, settings: Settings) -> Profile:
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < CACHE_TTL_S:
        return _cache[1]
    row = await session.scalar(select(ProcessingProfile).where(ProcessingProfile.active))
    profile = (
        default_profile(settings) if row is None else profile_from_json(row.version, row.settings)
    )
    _cache = (now, profile)
    return profile


def invalidate_profile_cache() -> None:
    global _cache
    _cache = None
