from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain import error_codes as ec
from tower.core.domain.errors import InvalidInput, NotFound, PermissionDenied
from tower.core.domain.factcheck.profiles import profile_to_json
from tower.core.domain.llm import month_period
from tower.core.domain.security import Actor, Permission, Role
from tower.core.factcheck import names
from tower.core.factcheck import profiles as profiles_cache
from tower.core.factcheck.profiles import active_profile, default_profile
from tower.core.llm.admin import _seed
from tower.core.models.events import Event
from tower.core.models.factcheck_v1 import ProcessingProfile
from tower.core.models.llm import BudgetCounter
from tower.core.usecases import profiles as uc

from .conftest import make_settings

SERVICE = Actor.service("intercom", frozenset(Permission))
ADMIN = Actor.staff(1003, frozenset({Role.ADMIN}), SERVICE)
EDITOR = Actor.staff(1002, frozenset({Role.EDITOR}), SERVICE)
REVIEWER = Actor.staff(1001, frozenset({Role.REVIEWER}), SERVICE)


async def events(db: Database, topic: str) -> list[Event]:
    async with db.session() as s:
        return list((await s.scalars(select(Event).where(Event.topic == topic))).all())


async def level_caps(db: Database) -> dict[str, int]:
    start, _ = month_period(datetime.now(UTC))
    async with db.session() as s:
        rows = await s.scalars(
            select(BudgetCounter).where(
                BudgetCounter.scope.like("level:%"), BudgetCounter.period_start == start
            )
        )
        return {r.scope: r.cap_usd_micros for r in rows}


@pytest.mark.parametrize(
    "changes",
    [
        {"match_same_threshold": 0.5},
        {"match_same_threshold": 1.0},
        {"match_grey_threshold": 0.4},
        {"match_grey_threshold": 0.95},
        {"draft_daily_quota": 501},
        {"draft_daily_quota": -1},
        {"e1_sample_rate": 0.3},
        {"e1_sample_rate": 0.001},
        {"levels.N1.e1_threshold": 1.5},
        {"levels.N1.monthly_budget_usd": -1},
        {"levels.N2.unit_cap_usd": 6.0, "levels.N2.monthly_budget_usd": 50},
        {"levels.N4.direct_reuse": True},
        {"levels.N4.e1_threshold": 0.1},
        {"levels.N3.e1_threshold": 0.5},
        {"levels.N2.monthly_budget_usd": 0.05},  # unit cap (0.10) above the budget
    ],
)
async def test_each_bound_is_refused(
    db: Database, settings: Settings, changes: dict[str, Any]
) -> None:
    async with db.transaction() as s:
        with pytest.raises(InvalidInput) as exc:
            await uc.create_profile_version(
                s, ADMIN, changes=changes, reason="test", app_settings=settings
            )
    assert exc.value.error_code is ec.FCK_PROFILE_OUT_OF_BOUNDS
    assert exc.value.details["violations"]
    async with db.session() as s:
        assert (await s.scalars(select(ProcessingProfile))).all() == []


async def test_unknown_path_and_bad_shapes_are_refused(db: Database, settings: Settings) -> None:
    async with db.transaction() as s:
        for changes in (
            {"levels.N9.e1_threshold": 0.1},
            {"levels.N1": 0.1},
            {"nope": 1},
            {"levels.N1.e1_threshold": "high"},
        ):
            with pytest.raises(InvalidInput) as exc:
                await uc.create_profile_version(
                    s, ADMIN, changes=changes, reason="x", app_settings=settings
                )
            assert exc.value.error_code is ec.FCK_PROFILE_OUT_OF_BOUNDS
        with pytest.raises(InvalidInput):
            await uc.create_profile_version(s, ADMIN, reason="x", app_settings=settings)
        with pytest.raises(InvalidInput):
            await uc.create_profile_version(
                s, ADMIN, changes={"draft_daily_quota": 5}, reason="   ", app_settings=settings
            )
        with pytest.raises(InvalidInput):
            await uc.create_profile_version(
                s, ADMIN, settings={"levels": {}}, reason="x", app_settings=settings
            )


async def test_version_numbering_and_changes(db: Database, settings: Settings) -> None:
    async with db.transaction() as s:
        v1 = await uc.create_profile_version(
            s, ADMIN, changes={"draft_daily_quota": 40}, reason="first", app_settings=settings
        )
        v2 = await uc.create_profile_version(
            s,
            ADMIN,
            changes={"levels.N1.e1_threshold": 0.2, "match_same_threshold": 0.95},
            reason="second",
            app_settings=settings,
        )
    assert (v1.version, v1.based_on_version, v1.active, v1.author) == (
        1,
        0,
        False,
        ADMIN.label,
    )
    assert v2.version == 2
    # Creating does not activate: the second version is still based on the default.
    assert v2.based_on_version == 0
    assert v2.settings["draft_daily_quota"] == settings.fc_draft_daily_quota
    assert v2.settings["levels"]["N1"]["e1_threshold"] == 0.2
    assert v2.settings["match_same_threshold"] == 0.95
    assert await events(db, names.PROCESSING_PROFILE_ACTIVATED) == []


async def test_full_settings_version(db: Database, settings: Settings) -> None:
    data = profile_to_json(default_profile(settings))
    data["draft_daily_quota"] = 7
    async with db.transaction() as s:
        row = await uc.create_profile_version(
            s, ADMIN, settings=data, reason="full", app_settings=settings
        )
    assert row.settings["draft_daily_quota"] == 7


async def test_activation_event_diff_caps_and_cache(db: Database, settings: Settings) -> None:
    profiles_cache.invalidate_profile_cache()
    async with db.session() as s:
        assert (await active_profile(s, settings)).version == 0  # fills the cache
    async with db.transaction() as s:
        row = await uc.create_profile_version(
            s,
            ADMIN,
            changes={"draft_daily_quota": 40, "levels.N3.monthly_budget_usd": 30},
            reason="more N3",
            activate=True,
            app_settings=settings,
        )
    assert row.active and row.activated_by == ADMIN.label and row.activated_at is not None
    # The per-process cache was invalidated: the new profile is seen at once.
    async with db.session() as s:
        profile = await active_profile(s, settings)
    assert profile.version == 1 and profile.draft_daily_quota == 40
    (event,) = await events(db, names.PROCESSING_PROFILE_ACTIVATED)
    assert event.payload["version"] == 1 and event.payload["previous_version"] == 0
    assert event.payload["actor"] == ADMIN.label and event.payload["reason"] == "more N3"
    assert event.payload["changes"] == {
        "draft_daily_quota": [settings.fc_draft_daily_quota, 40],
        "levels.N3.monthly_budget_usd": [20.0, 30.0],
    }
    caps = await level_caps(db)
    assert caps["level:N3"] == 30_000_000 and caps["level:N1"] == 5_000_000
    assert set(caps) == {f"level:N{i}" for i in range(5)}

    # A second version replaces the first: only one row stays active.
    async with db.transaction() as s:
        await uc.create_profile_version(
            s,
            ADMIN,
            changes={"draft_daily_quota": 10},
            reason="less",
            activate=True,
            app_settings=settings,
        )
    async with db.session() as s:
        rows = {r.version: r.active for r in await s.scalars(select(ProcessingProfile))}
    assert rows == {1: False, 2: True}
    # Re-activating the active version is a no-op (no extra event).
    async with db.transaction() as s:
        await uc.activate_profile(s, ADMIN, version=2, reason="again", app_settings=settings)
    assert len(await events(db, names.PROCESSING_PROFILE_ACTIVATED)) == 2
    profiles_cache.invalidate_profile_cache()


async def test_activate_unknown_version(db: Database, settings: Settings) -> None:
    async with db.transaction() as s:
        with pytest.raises(NotFound) as exc:
            await uc.activate_profile(s, ADMIN, version=42, reason="x", app_settings=settings)
    assert exc.value.error_code is ec.FCK_PROFILE_NOT_FOUND


async def test_rollback_to_previous_then_default(db: Database, settings: Settings) -> None:
    async with db.transaction() as s:
        await uc.create_profile_version(
            s,
            ADMIN,
            changes={"draft_daily_quota": 11},
            reason="a",
            activate=True,
            app_settings=settings,
        )
    async with db.transaction() as s:
        await uc.create_profile_version(
            s,
            ADMIN,
            changes={"draft_daily_quota": 12},
            reason="b",
            activate=True,
            app_settings=settings,
        )
    async with db.transaction() as s:
        back = await uc.rollback_profile(s, ADMIN, reason="oops", app_settings=settings)
    assert back.version == 1
    async with db.transaction() as s:
        back = await uc.rollback_profile(s, ADMIN, reason="oops again", app_settings=settings)
    assert back.version == 2  # most recently activated inactive version
    # Explicit return to the default profile (version 0).
    async with db.transaction() as s:
        default = await uc.activate_profile(
            s, ADMIN, version=0, reason="reset", app_settings=settings
        )
    assert default.version == 0
    async with db.session() as s:
        assert not any(r.active for r in await s.scalars(select(ProcessingProfile)))
    assert (await events(db, names.PROCESSING_PROFILE_ACTIVATED))[-1].payload["version"] == 0
    # The default caps are back on the counters.
    assert (await level_caps(db))["level:N3"] == 20_000_000
    profiles_cache.invalidate_profile_cache()


async def test_rollback_without_history_goes_to_default(db: Database, settings: Settings) -> None:
    async with db.transaction() as s:
        await uc.create_profile_version(
            s,
            ADMIN,
            changes={"draft_daily_quota": 11},
            reason="a",
            activate=True,
            app_settings=settings,
        )
    async with db.transaction() as s:
        back = await uc.rollback_profile(s, ADMIN, reason="undo", app_settings=settings)
    assert back.version == 0
    profiles_cache.invalidate_profile_cache()


@pytest.mark.parametrize("actor", [REVIEWER, EDITOR])
async def test_only_admin_manages_profiles(db: Database, settings: Settings, actor: Actor) -> None:
    async with db.transaction() as s:
        with pytest.raises(PermissionDenied):
            await uc.create_profile_version(
                s, actor, changes={"draft_daily_quota": 1}, reason="x", app_settings=settings
            )
        with pytest.raises(PermissionDenied):
            await uc.activate_profile(s, actor, version=0, reason="x", app_settings=settings)
        with pytest.raises(PermissionDenied):
            await uc.rollback_profile(s, actor, reason="x", app_settings=settings)
        # Reading is allowed for reviewers and editors.
        assert (await uc.current_profile(s, actor, app_settings=settings)).version == 0
        assert await uc.list_profiles(s, actor) == []


async def test_admin_can_manage(db: Database, settings: Settings) -> None:
    async with db.transaction() as s:
        row = await uc.create_profile_version(
            s,
            ADMIN,
            changes={"draft_daily_quota": 1},
            reason="x",
            activate=True,
            app_settings=settings,
        )
    assert row.active
    profiles_cache.invalidate_profile_cache()


async def test_seed_creates_level_scopes_once(db: Database) -> None:
    cfg = make_settings()
    now = datetime.now(UTC)
    first = await _seed(db, cfg, now)
    second = await _seed(db, cfg, now)
    assert first["level_budgets_created"] == 5 and second["level_budgets_created"] == 0
    caps = await level_caps(db)
    assert caps == {
        "level:N0": 1_000_000,
        "level:N1": 5_000_000,
        "level:N2": 10_000_000,
        "level:N3": 20_000_000,
        "level:N4": 15_000_000,
    }


async def test_draft_quota_is_read_from_the_profile(db: Database) -> None:
    from tower.services.factcheck import assembly as asm
    from tower.services.factcheck.periodic import drain_quota

    cfg = make_settings(fc_draft_daily_quota=0)
    profiles_cache.invalidate_profile_cache()
    async with db.transaction() as s:
        assert await drain_quota(s, cfg) == 0  # nothing waiting anyway
        await uc.create_profile_version(
            s, ADMIN, changes={"draft_daily_quota": 3}, reason="x", activate=True, app_settings=cfg
        )
        assert (await active_profile(s, cfg)).draft_daily_quota == 3
        assert await asm.today_count(s) == 0
    profiles_cache.invalidate_profile_cache()


def test_cap_micros_reads_the_profile_caps() -> None:
    from tower.core.domain.factcheck.values import Depth
    from tower.services.investigate import pipeline as pl

    cfg = make_settings()
    profile = default_profile(cfg)
    assert pl.cap_micros(cfg, "N2", Depth.STANDARD, profile) == 100_000
    assert pl.cap_micros(cfg, "N4", Depth.REINFORCED, profile) == 600_000
    # Reinforced depth outside N4 keeps fc_cap_reinforced_usd, whatever the level cap.
    assert pl.cap_micros(cfg, "N2", Depth.REINFORCED, profile) == 400_000
    from dataclasses import replace

    levels = dict(profile.levels)
    levels["N2"] = replace(levels["N2"], unit_cap_usd=0.25)
    levels["N4"] = replace(levels["N4"], unit_cap_usd=1.0)
    changed = replace(profile, levels=levels)
    assert pl.cap_micros(cfg, "N2", Depth.STANDARD, changed) == 250_000
    assert pl.cap_micros(cfg, "N4", Depth.STANDARD, changed) == 1_000_000
