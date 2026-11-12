"""E4 scheduler: adaptive passes, group metrics (SQL aggregates), promotions confirmed by a
platform read, demotions, priority order, budget / quota / capacity gates, light-check share,
passive transition and the run journal. Real PostgreSQL; fake platform client."""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain.security import Actor
from tower.core.factcheck import names, runtime
from tower.core.factcheck.profiles import invalidate_profile_cache
from tower.core.jobs.queue import PermanentError, enqueue
from tower.core.models.factcheck import (
    Claim,
    ClaimGroup,
    ClaimGroupMember,
    ContentClaim,
    Investigation,
)
from tower.core.ports.platforms import (
    ContentItem as PlatformItem,
)
from tower.core.ports.platforms import (
    Metrics,
    PlatformUnavailable,
    ThreadContext,
)
from tower.services.factcheck import narratives
from tower.services.factcheck.scheduler import promotion_check, schedule

from .conftest import make_settings
from .test_factcheck_matching import events_of, give_verdict
from .test_factcheck_service import ctx, make_content, rows, scalar

SYSTEM = Actor.system("test")


# ------------------------------------------------------------------------- doubles


class MetricsPlatform:
    platform = "x"
    cost_per_read_usd_micros = 5_000

    def __init__(self, views: int | None = None, error: Exception | None = None) -> None:
        self.views = views
        self.error = error
        self.calls: list[str] = []

    async def get_item(self, external_id: str) -> PlatformItem | None:
        return None

    async def get_thread_context(self, item: PlatformItem) -> ThreadContext:
        return ThreadContext()

    async def get_metrics(self, external_id: str) -> Metrics | None:
        self.calls.append(external_id)
        if self.error is not None:
            raise self.error
        if self.views is None:
            return None
        return Metrics(values={"impression_count": self.views}, measured_at=datetime.now(UTC))

    async def get_account(self, account_ref: str) -> None:
        return None

    async def poll_account(self, account_ref: str, *, since: str | None) -> Sequence[PlatformItem]:
        return []


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    runtime.clear_overrides()
    invalidate_profile_cache()
    yield
    runtime.clear_overrides()
    invalidate_profile_cache()


# ------------------------------------------------------------------------- seeding


async def make_group(
    db: Database,
    ext: str,
    *,
    level: str = "N1",
    state: str = "dormant",
    theme: str = "sport",
    verifiability: float = 0.8,
    views: int | None = None,
    created_ago: timedelta | None = None,
    updated_ago: timedelta | None = None,
) -> tuple[int, int]:
    """A group with one claim and one content; returns ``(group_id, content_id)``."""
    metrics = {"impression_count": views} if views is not None else None
    content_id = await make_content(db, ext, f"Claim text {ext}", metrics=metrics)
    async with db.transaction() as session:
        claim = Claim(
            public_id=uuid.uuid4(),
            lang_original="fr",
            text_original=f"Claim text {ext}",
            text_fr=f"Claim text {ext}",
            claim_type="statistic",
            theme=theme,
            verifiability=verifiability,
            prompt_version="pv1",
        )
        session.add(claim)
        await session.flush()
        group = ClaimGroup(
            public_id=uuid.uuid4(),
            representative_claim_id=claim.id,
            level=level,
            state=state,
            reach=views or 0,
        )
        session.add(group)
        await session.flush()
        session.add(ContentClaim(content_id=content_id, claim_id=claim.id, position=0))
        session.add(ClaimGroupMember(group_id=group.id, claim_id=claim.id))
        await session.flush()
        if created_ago is not None:
            await session.execute(
                text("UPDATE claim_groups SET created_at = :t WHERE id = :g"),
                {"t": datetime.now(UTC) - created_ago, "g": group.id},
            )
        if updated_ago is not None:
            await session.execute(
                text("UPDATE claim_groups SET updated_at = :t WHERE id = :g"),
                {"t": datetime.now(UTC) - updated_ago, "g": group.id},
            )
        return int(group.id), content_id


async def observe(
    db: Database,
    content_id: int,
    *,
    minutes_ago: int,
    pseudonyms: Sequence[bytes | None] = (None,),
) -> None:
    async with db.transaction() as session:
        for pseudonym in pseudonyms:
            await session.execute(
                text(
                    "INSERT INTO observations (observed_at, content_id, platform, source,"
                    " installation_pseudonym) VALUES (:t, :c, 'x', 'extension', :p)"
                ),
                {
                    "t": datetime.now(UTC) - timedelta(minutes=minutes_ago),
                    "c": content_id,
                    "p": pseudonym,
                },
            )


def pseudo(n: int) -> bytes:
    return n.to_bytes(16, "big")


async def run_pass(db: Database, settings: Settings) -> int:
    async with db.transaction() as session:
        return await schedule(session, settings)


async def last_runs(db: Database) -> list[Any]:
    return await rows(
        db,
        "SELECT id, started_at, finished_at, rate_per_hour, interval_s, stats"
        " FROM scheduler_runs ORDER BY id",
    )


async def group_row(db: Database, group_id: int) -> Any:
    return (
        await rows(
            db,
            "SELECT level, state, observers_6h, velocity, reach, priority, light_signal,"
            " below_threshold_since FROM claim_groups WHERE id = :i",
            i=group_id,
        )
    )[0]


async def jobs(db: Database, kind: str) -> list[dict[str, Any]]:
    result = await rows(
        db, "SELECT payload, dedupe_key FROM jobs WHERE kind = :k ORDER BY id", k=kind
    )
    return [{**r[0], "_dedupe": r[1]} for r in result]


async def seed_run(db: Database, *, ago: timedelta) -> None:
    async with db.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO scheduler_runs (started_at, rate_per_hour, interval_s)"
                " VALUES (:t, 0, 60)"
            ),
            {"t": datetime.now(UTC) - ago},
        )


@pytest.fixture
def settings_(settings: Settings) -> Settings:
    return settings


# ------------------------------------------------------------------------- interval


async def test_first_pass_runs_and_writes_one_journal_row(db: Database, settings: Settings) -> None:
    await run_pass(db, settings)
    runs = await last_runs(db)
    assert len(runs) == 1
    assert runs[0][2] is not None  # finished_at
    stats = runs[0][5]
    assert {"rate", "interval_s", "promotions", "demotions", "enqueued", "held_back", "waves"} <= (
        set(stats)
    )
    assert isinstance(stats["waves"], dict)


async def test_pass_is_skipped_while_the_interval_has_not_elapsed(
    db: Database, settings: Settings
) -> None:
    await run_pass(db, settings)
    assert await run_pass(db, settings) == 0
    assert len(await last_runs(db)) == 1


async def test_quiet_flow_waits_the_maximum_interval(db: Database, settings: Settings) -> None:
    longest = settings.fc_scheduler_max_interval_s
    await seed_run(db, ago=timedelta(seconds=longest - 120))
    await run_pass(db, settings)
    assert len(await last_runs(db)) == 1  # still waiting
    async with db.transaction() as session:
        await session.execute(text("DELETE FROM scheduler_runs"))
    await seed_run(db, ago=timedelta(seconds=longest + 60))
    await run_pass(db, settings)
    assert len(await last_runs(db)) == 2  # the seeded row, then a real pass


async def test_interval_follows_the_flow(db: Database) -> None:
    settings = make_settings(fc_scheduler_target_per_pass=0.5, fc_scheduler_min_interval_s=60)
    for i in range(3):
        await make_content(db, f"90{i}", f"recent post {i}")
    # 3 contents in the last hour: 3600 x 0.5 / 3 = 600 s between two passes.
    await seed_run(db, ago=timedelta(seconds=300))
    await run_pass(db, settings)
    assert len(await last_runs(db)) == 1  # still waiting
    async with db.transaction() as session:
        await session.execute(text("DELETE FROM scheduler_runs"))
    await seed_run(db, ago=timedelta(seconds=700))
    await run_pass(db, settings)
    runs = await last_runs(db)
    assert len(runs) == 2
    assert runs[1][3] == pytest.approx(3.0) and runs[1][4] == 600


async def test_waves_step_result_is_stored_in_the_run(
    db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_waves(session: Any, settings: Settings, now: datetime) -> dict[str, Any]:
        return {"surge": 1}

    monkeypatch.setattr(narratives, "waves_step", fake_waves)
    await run_pass(db, settings)
    assert (await last_runs(db))[0][5]["waves"] == {"surge": 1}


# ------------------------------------------------------------------------- metrics


async def test_metrics_are_aggregated_in_sql(db: Database, settings: Settings) -> None:
    gid, cid = await make_group(db, "100", views=1200)
    gid2, _ = await make_group(db, "101")
    # second content of the same group, with more views
    other = await make_content(db, "102", "second post", metrics={"views": 4000})
    async with db.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO content_claims (content_id, claim_id, position) SELECT :c, claim_id,"
                " 1 FROM content_claims WHERE content_id = :o"
            ),
            {"c": other, "o": cid},
        )
    await observe(db, cid, minutes_ago=30, pseudonyms=[pseudo(1), pseudo(2), pseudo(2), None])
    await observe(db, other, minutes_ago=200, pseudonyms=[pseudo(3)])
    await observe(db, cid, minutes_ago=60 * 7, pseudonyms=[pseudo(4)])  # outside 6 h
    await observe(db, cid, minutes_ago=80, pseudonyms=[pseudo(5), pseudo(5)])  # previous hour

    await run_pass(db, settings)
    row = await group_row(db, gid)
    assert row.observers_6h == 4  # pseudonyms 1, 2, 3, 5 (4 is too old, NULL is ignored)
    assert row.velocity == 0  # fewer than 10 observations in the last hour
    assert row.reach == 4000  # max over the group's contents (impression_count, views)
    other_row = await group_row(db, gid2)
    assert other_row.observers_6h == 0


async def test_velocity_is_the_ratio_of_the_last_two_hours(
    db: Database, settings: Settings
) -> None:
    gid, cid = await make_group(db, "110")
    await observe(db, cid, minutes_ago=10, pseudonyms=[None] * 11)  # 12 with the ingestion
    await observe(db, cid, minutes_ago=90, pseudonyms=[None] * 3)
    await run_pass(db, settings)
    assert (await group_row(db, gid)).velocity == pytest.approx(4.0)


async def test_stale_metrics_are_zeroed_and_old_groups_ignored(
    db: Database, settings: Settings
) -> None:
    gid, _ = await make_group(db, "120")
    old, old_cid = await make_group(db, "121", updated_ago=timedelta(hours=60))
    await observe(db, old_cid, minutes_ago=5, pseudonyms=[pseudo(i) for i in range(8)])
    async with db.transaction() as session:
        await session.execute(
            text("UPDATE claim_groups SET observers_6h = 9, velocity = 2 WHERE id = :g"),
            {"g": gid},
        )
    await run_pass(db, settings)
    assert (await group_row(db, gid)).observers_6h == 0  # no observation left in 6 h
    assert (await group_row(db, old)).observers_6h == 0  # inactive for 48 h: not computed


async def test_merged_and_passive_groups_are_not_scheduled(
    db: Database, settings: Settings
) -> None:
    merged, cid = await make_group(db, "130", state="merged")
    await observe(db, cid, minutes_ago=5, pseudonyms=[pseudo(i) for i in range(8)])
    await run_pass(db, settings)
    assert (await group_row(db, merged)).observers_6h == 0
    assert await jobs(db, names.JOB_PROMOTION_CHECK) == []


# ------------------------------------------------------------------------ promotions


async def promotion_jobs(db: Database) -> list[dict[str, Any]]:
    return await jobs(db, names.JOB_PROMOTION_CHECK)


async def test_observers_reason_enqueues_a_promotion_check(
    db: Database, settings: Settings
) -> None:
    gid, cid = await make_group(db, "200")
    await observe(db, cid, minutes_ago=20, pseudonyms=[pseudo(i) for i in range(5)])
    await run_pass(db, settings)
    found = await promotion_jobs(db)
    assert len(found) == 1
    assert found[0]["group_id"] == gid and found[0]["reasons"] == ["observers"]
    assert found[0]["_dedupe"].startswith(f"promo:{gid}:")
    assert (await group_row(db, gid)).level == "N1"  # confirmation comes first


async def test_four_observers_do_not_promote(db: Database, settings: Settings) -> None:
    _, cid = await make_group(db, "201")
    await observe(db, cid, minutes_ago=20, pseudonyms=[pseudo(i) for i in range(4)])
    await run_pass(db, settings)
    assert await promotion_jobs(db) == []


async def test_velocity_reason(db: Database, settings: Settings) -> None:
    _, cid = await make_group(db, "202")
    await observe(db, cid, minutes_ago=10, pseudonyms=[None] * 11)
    await observe(db, cid, minutes_ago=90, pseudonyms=[None] * 3)
    await run_pass(db, settings)
    assert [j["reasons"] for j in await promotion_jobs(db)] == [["velocity"]]


async def test_views_reason(db: Database, settings: Settings) -> None:
    await make_group(db, "203", views=60_000)
    await make_group(db, "204", views=50_000)  # not strictly above
    await run_pass(db, settings)
    assert [j["reasons"] for j in await promotion_jobs(db)] == [["views"]]


async def test_light_signal_reason_needs_reach_above_the_median(
    db: Database, settings: Settings
) -> None:
    high, _ = await make_group(db, "205", views=5000)
    low, _ = await make_group(db, "206", views=10)
    mid, _ = await make_group(db, "207", views=100)
    async with db.transaction() as session:
        await session.execute(text("UPDATE claim_groups SET light_signal = 'likely_false'"))
    await run_pass(db, settings)
    found = await promotion_jobs(db)
    assert [(j["group_id"], j["reasons"]) for j in found] == [(high, ["light_signal"])]
    assert low and mid


async def test_narrative_reason(db: Database, settings: Settings) -> None:
    gid, _ = await make_group(db, "208")
    await make_narrative(db, gid, "surge")
    await run_pass(db, settings)
    assert [j["reasons"] for j in await promotion_jobs(db)] == [["narrative"]]


async def make_narrative(db: Database, group_id: int, state: str) -> int:
    async with db.transaction() as session:
        narrative_id = int(
            await session.scalar(
                text(
                    "INSERT INTO narratives (public_id, state, founder_group_id,"
                    " medoid_group_ids) VALUES (gen_random_uuid(), :s, :g, ARRAY[:g]::bigint[])"
                    " RETURNING id"
                ),
                {"s": state, "g": group_id},
            )
            or 0
        )
        await session.execute(
            text("INSERT INTO narrative_groups (narrative_id, group_id) VALUES (:n, :g)"),
            {"n": narrative_id, "g": group_id},
        )
    return narrative_id


async def test_promotion_check_is_deduplicated_within_the_hour(
    db: Database, settings: Settings
) -> None:
    _, cid = await make_group(db, "209")
    await observe(db, cid, minutes_ago=20, pseudonyms=[pseudo(i) for i in range(5)])
    await run_pass(db, settings)
    async with db.transaction() as session:
        await session.execute(text("DELETE FROM scheduler_runs"))
    await run_pass(db, settings)
    assert len(await promotion_jobs(db)) == 1


# --------------------------------------------------------------- promotion_check job


def check_ctx(db: Database, settings: Settings, group_id: int, reasons: list[str]) -> Any:
    return ctx(
        db,
        settings,
        names.JOB_PROMOTION_CHECK,
        {"group_id": group_id, "reasons": reasons},
    )


async def test_promotion_confirmed_by_fresh_metrics(db: Database, settings: Settings) -> None:
    gid, _ = await make_group(db, "300", views=60_000)
    platform = MetricsPlatform(views=61_000)
    runtime.set_override("platform:x", platform)
    await promotion_check(check_ctx(db, settings, gid, ["views"]))

    assert (await group_row(db, gid)).level == "N2"
    assert platform.calls == ["300"]
    changed = await events_of(db, names.CLAIM_GROUP_LEVEL_CHANGED)
    assert changed[0]["reason"] == "promotion"
    assert (changed[0]["from_level"], changed[0]["to_level"]) == ("N1", "N2")
    assert "unconfirmed" not in changed[0]
    journal = await rows(
        db,
        "SELECT kind, from_level, to_level, reason FROM claim_group_events WHERE group_id = :g",
        g=gid,
    )
    assert journal[-1] == ("level_changed", "N1", "N2", "promotion")
    assert await scalar(db, "SELECT level_changed_at IS NOT NULL FROM claim_groups")


async def test_promotion_not_confirmed_below_threshold(db: Database, settings: Settings) -> None:
    gid, _ = await make_group(db, "301", views=60_000)
    runtime.set_override("platform:x", MetricsPlatform(views=100))
    await promotion_check(check_ctx(db, settings, gid, ["views"]))
    assert (await group_row(db, gid)).level == "N1"
    assert await events_of(db, names.CLAIM_GROUP_LEVEL_CHANGED) == []


async def test_non_metric_reason_stands_whatever_the_platform_says(
    db: Database, settings: Settings
) -> None:
    gid, _ = await make_group(db, "302")
    runtime.set_override("platform:x", MetricsPlatform(views=3))
    await promotion_check(check_ctx(db, settings, gid, ["observers", "views"]))
    assert (await group_row(db, gid)).level == "N2"


async def test_unavailable_platform_promotes_unconfirmed(db: Database, settings: Settings) -> None:
    gid, _ = await make_group(db, "303")
    # No override and no bearer token: the platform client cannot be built.
    await promotion_check(check_ctx(db, settings, gid, ["observers"]))
    assert (await group_row(db, gid)).level == "N2"
    changed = await events_of(db, names.CLAIM_GROUP_LEVEL_CHANGED)
    assert changed[0]["unconfirmed"] is True
    data = await scalar(
        db,
        "SELECT data FROM claim_group_events WHERE kind = 'level_changed' AND group_id = :g",
        g=gid,
    )
    assert data == {"unconfirmed": True}


async def test_platform_failure_does_not_promote(db: Database, settings: Settings) -> None:
    gid, _ = await make_group(db, "304")
    runtime.set_override("platform:x", MetricsPlatform(error=PlatformUnavailable("down")))
    await promotion_check(check_ctx(db, settings, gid, ["observers"]))
    assert (await group_row(db, gid)).level == "N1"


async def test_missing_post_does_not_promote(db: Database, settings: Settings) -> None:
    gid, _ = await make_group(db, "305")
    runtime.set_override("platform:x", MetricsPlatform(views=None))
    await promotion_check(check_ctx(db, settings, gid, ["views"]))
    assert (await group_row(db, gid)).level == "N1"


async def test_refused_provider_budget_does_not_promote(db: Database, settings: Settings) -> None:
    gid, _ = await make_group(db, "306")
    platform = MetricsPlatform(views=99_999)
    runtime.set_override("platform:x", platform)
    async with db.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO budget_counters (scope, period_start, period_end, cap_usd_micros)"
                " VALUES ('provider:x', date_trunc('month', now())::date,"
                " (date_trunc('month', now()) + interval '1 month')::date, 1)"
            )
        )
    await promotion_check(check_ctx(db, settings, gid, ["views"]))
    assert (await group_row(db, gid)).level == "N1"
    assert platform.calls == []


async def test_read_cost_is_settled_on_provider_x(db: Database, settings: Settings) -> None:
    gid, _ = await make_group(db, "307", views=60_000)
    runtime.set_override("platform:x", MetricsPlatform(views=70_000))
    async with db.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO budget_counters (scope, period_start, period_end, cap_usd_micros)"
                " VALUES ('provider:x', date_trunc('month', now())::date,"
                " (date_trunc('month', now()) + interval '1 month')::date, 1000000)"
            )
        )
    await promotion_check(check_ctx(db, settings, gid, ["views"]))
    spent, reserved = (
        await rows(db, "SELECT spent_usd_micros, reserved_usd_micros FROM budget_counters")
    )[0]
    assert (spent, reserved) == (5_000, 0)


async def test_promotion_check_is_idempotent_and_ignores_other_levels(
    db: Database, settings: Settings
) -> None:
    gid, _ = await make_group(db, "308")
    runtime.set_override("platform:x", MetricsPlatform(views=99_999))
    await promotion_check(check_ctx(db, settings, gid, ["views"]))
    await promotion_check(check_ctx(db, settings, gid, ["views"]))
    assert len(await events_of(db, names.CLAIM_GROUP_LEVEL_CHANGED)) == 1
    n3, _ = await make_group(db, "309", level="N3")
    await promotion_check(check_ctx(db, settings, n3, ["views"]))
    assert (await group_row(db, n3)).level == "N3"


async def test_promotion_check_rejects_a_bad_payload(db: Database, settings: Settings) -> None:
    with pytest.raises(PermanentError):
        await promotion_check(ctx(db, settings, names.JOB_PROMOTION_CHECK, {"reasons": ["views"]}))
    with pytest.raises(PermanentError):
        await promotion_check(
            ctx(db, settings, names.JOB_PROMOTION_CHECK, {"group_id": 1, "reasons": []})
        )


# ------------------------------------------------------------------------ demotions


async def set_below(db: Database, group_id: int, ago: timedelta) -> None:
    async with db.transaction() as session:
        await session.execute(
            text("UPDATE claim_groups SET below_threshold_since = :t WHERE id = :g"),
            {"t": datetime.now(UTC) - ago, "g": group_id},
        )


async def test_demotion_after_the_delay(db: Database, settings: Settings) -> None:
    gid, _ = await make_group(db, "400", level="N2")
    await set_below(db, gid, timedelta(hours=25))
    await run_pass(db, settings)
    row = await group_row(db, gid)
    assert row.level == "N1" and row.below_threshold_since is None
    changed = await events_of(db, names.CLAIM_GROUP_LEVEL_CHANGED)
    assert (changed[0]["from_level"], changed[0]["to_level"], changed[0]["reason"]) == (
        "N2",
        "N1",
        "demotion",
    )
    journal = await rows(
        db, "SELECT kind, reason FROM claim_group_events WHERE group_id = :g", g=gid
    )
    assert journal[-1] == ("level_changed", "demotion")


async def test_demotion_clock_starts_then_waits(db: Database, settings: Settings) -> None:
    fresh, _ = await make_group(db, "401", level="N2")
    recent, _ = await make_group(db, "402", level="N2")
    await set_below(db, recent, timedelta(hours=3))
    await run_pass(db, settings)
    assert (await group_row(db, fresh)).below_threshold_since is not None
    assert (await group_row(db, fresh)).level == "N2"
    assert (await group_row(db, recent)).level == "N2"


async def test_no_demotion_during_an_investigation(db: Database, settings: Settings) -> None:
    gid, cid = await make_group(db, "403", level="N2", state="investigating")
    async with db.transaction() as session:
        session.add(
            Investigation(
                public_id=uuid.uuid4(),
                claim_group_id=gid,
                content_id=cid,
                level="N2",
                depth="standard",
                stage="judging",
                requested_by="test",
            )
        )
    await set_below(db, gid, timedelta(hours=40))
    await run_pass(db, settings)
    assert (await group_row(db, gid)).level == "N2"


async def test_reached_group_clears_its_demotion_clock(db: Database, settings: Settings) -> None:
    gid, cid = await make_group(db, "404", level="N2")
    await set_below(db, gid, timedelta(hours=30))
    await observe(db, cid, minutes_ago=10, pseudonyms=[pseudo(i) for i in range(6)])
    await run_pass(db, settings)
    row = await group_row(db, gid)
    assert row.level == "N2" and row.below_threshold_since is None


# ---------------------------------------------------------------------- priority


async def test_n2_groups_are_requested_in_priority_order(db: Database, settings: Settings) -> None:
    low, _ = await make_group(db, "500", level="N2", theme="sport", views=100)
    high, _ = await make_group(db, "501", level="N2", theme="health", views=30_000)
    mid, _ = await make_group(db, "502", level="N2", theme="economy", views=3000)
    await run_pass(db, settings)

    order = [r[0] for r in await rows(db, "SELECT claim_group_id FROM investigations ORDER BY id")]
    assert order == [high, mid, low]
    priorities = [(await group_row(db, g)).priority for g in (high, mid, low)]
    assert priorities[0] > priorities[1] > priorities[2] > 0
    inv = (await rows(db, "SELECT level, depth, flags, requested_by FROM investigations"))[0]
    assert inv[0] == "N2" and inv[1] == "standard" and inv[2] == {"scheduled": True}
    assert (await group_row(db, high)).state == "queued"
    assert len(await events_of(db, names.INVESTIGATION_REQUESTED)) == 3


async def test_priority_ignores_the_author(db: Database, settings: Settings) -> None:
    a, _ = await make_group(db, "510", level="N2", views=500)
    b, cid = await make_group(db, "511", level="N2", views=500)
    async with db.transaction() as session:
        await session.execute(
            text("UPDATE content_items SET author_account_id = NULL WHERE id = :c"), {"c": cid}
        )
    await run_pass(db, settings)
    assert (await group_row(db, a)).priority == pytest.approx((await group_row(db, b)).priority)


async def test_a_group_with_an_investigation_is_not_requested_again(
    db: Database, settings: Settings
) -> None:
    gid, cid = await make_group(db, "520", level="N2")
    async with db.transaction() as session:
        session.add(
            Investigation(
                public_id=uuid.uuid4(),
                claim_group_id=gid,
                content_id=cid,
                level="N2",
                depth="standard",
                stage="failed",
                requested_by="test",
            )
        )
    await run_pass(db, settings)
    assert await scalar(db, "SELECT count(*) FROM investigations") == 1


async def test_groups_of_a_surging_narrative_are_left_to_the_waves_step(
    db: Database, settings: Settings
) -> None:
    gid, _ = await make_group(db, "530", level="N2")
    await make_narrative(db, gid, "surge")
    await run_pass(db, settings)
    assert await scalar(db, "SELECT count(*) FROM investigations") == 0


# ---------------------------------------------------------------------------- gates


async def test_level_budget_gate_holds_back(db: Database, settings: Settings) -> None:
    await make_group(db, "600", level="N2")
    async with db.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO budget_counters (scope, period_start, period_end, cap_usd_micros,"
                " spent_usd_micros) VALUES ('level:N2', date_trunc('month', now())::date,"
                " (date_trunc('month', now()) + interval '1 month')::date, 100000, 95000)"
            )
        )
    await run_pass(db, settings)
    assert await scalar(db, "SELECT count(*) FROM investigations") == 0
    stats = (await last_runs(db))[0][5]
    assert stats["held_back"] == 1 and stats["held_back_by"]["budget"] == 1


async def test_level_budget_is_spent_down_within_a_pass(db: Database, settings: Settings) -> None:
    for i in range(3):
        await make_group(db, f"61{i}", level="N2", views=100 * (i + 1))
    async with db.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO budget_counters (scope, period_start, period_end, cap_usd_micros)"
                " VALUES ('level:N2', date_trunc('month', now())::date,"
                " (date_trunc('month', now()) + interval '1 month')::date, 25000)"
            )
        )
    await run_pass(db, settings)  # mean cost floored at 1 cent: two investigations fit
    assert await scalar(db, "SELECT count(*) FROM investigations") == 2
    assert (await last_runs(db))[0][5]["held_back"] == 1


async def test_uncapped_level_budget_does_not_hold_back(db: Database, settings: Settings) -> None:
    await make_group(db, "620", level="N2")
    await run_pass(db, settings)
    assert await scalar(db, "SELECT count(*) FROM investigations") == 1


async def test_daily_draft_quota_gate(db: Database) -> None:
    settings = make_settings(fc_draft_daily_quota=0)
    await make_group(db, "630", level="N2")
    await run_pass(db, settings)
    assert await scalar(db, "SELECT count(*) FROM investigations") == 0
    assert (await last_runs(db))[0][5]["held_back_by"]["quota"] == 1


async def test_daily_draft_quota_counts_todays_drafts(db: Database) -> None:
    settings = make_settings(fc_draft_daily_quota=1)
    done, _ = await make_group(db, "631", level="N1")
    await give_verdict(db, done)
    async with db.transaction() as session:
        await session.execute(text("UPDATE verdicts SET drafted_at = now()"))
    await make_group(db, "632", level="N2")
    await run_pass(db, settings)
    assert await scalar(db, "SELECT count(*) FROM investigations") == 0


async def test_deep_queue_capacity_gate(db: Database) -> None:
    settings = make_settings(fc_deep_queue_capacity=2)
    async with db.transaction() as session:
        await enqueue(session, names.FC_DEEP, "investigate.frame", {"x": 1}, dedupe_key="a")
    for i in range(3):
        await make_group(db, f"64{i}", level="N2", views=10 * (i + 1))
    await run_pass(db, settings)
    assert await scalar(db, "SELECT count(*) FROM investigations") == 1  # 1 queued + 1 = 2
    stats = (await last_runs(db))[0][5]
    assert stats["held_back_by"]["capacity"] == 2 and stats["held_back"] == 2
    assert stats["investigations"] == 1


# ------------------------------------------------------------------- light checks


async def test_light_check_goes_to_the_top_share_of_new_n1_groups(db: Database) -> None:
    settings = make_settings(fc_light_check_share=0.2)
    ids: list[int] = []
    for i in range(5):
        gid, _ = await make_group(db, f"70{i}", views=10 ** (i + 1) if i < 4 else 20_000)
        ids.append(gid)
    await run_pass(db, settings)
    found = await jobs(db, names.JOB_LIGHT_CHECK)
    assert [j["group_id"] for j in found] == [ids[4]]  # 20 % of 5 = 1, the highest priority
    assert found[0]["dossier_investigation_id"] is None
    assert found[0]["_dedupe"] == names.light_check_key(ids[4], 0)
    assert (await last_runs(db))[0][5]["light_checks"] == 1


async def test_light_check_share_rounds_up(db: Database) -> None:
    settings = make_settings(fc_light_check_share=0.5)
    for i in range(3):
        await make_group(db, f"71{i}", views=10 * (i + 1))
    await run_pass(db, settings)
    assert len(await jobs(db, names.JOB_LIGHT_CHECK)) == 2


async def test_no_light_check_when_the_share_is_zero(db: Database) -> None:
    settings = make_settings(fc_light_check_share=0)
    await make_group(db, "720")
    await run_pass(db, settings)
    assert await jobs(db, names.JOB_LIGHT_CHECK) == []


async def test_only_groups_new_since_the_last_pass_are_light_checked(db: Database) -> None:
    settings = make_settings(fc_light_check_share=1.0)
    await seed_run(db, ago=timedelta(hours=2))
    await make_group(db, "730", created_ago=timedelta(hours=5))
    fresh, _ = await make_group(db, "731")
    already, _ = await make_group(db, "732")
    async with db.transaction() as session:
        await session.execute(
            text("UPDATE claim_groups SET light_checked_at = now() WHERE id = :g"), {"g": already}
        )
    await run_pass(db, settings)
    assert [j["group_id"] for j in await jobs(db, names.JOB_LIGHT_CHECK)] == [fresh]


async def test_n1_priority_is_written_but_n1_never_investigated(
    db: Database, settings: Settings
) -> None:
    gid, _ = await make_group(db, "740", views=1000)
    await run_pass(db, settings)
    assert (await group_row(db, gid)).priority > 0
    assert await scalar(db, "SELECT count(*) FROM investigations") == 0


# --------------------------------------------------------------------------- passive


async def test_old_verified_groups_become_passive(db: Database, settings: Settings) -> None:
    old, _ = await make_group(db, "800", state="verified")
    recent, _ = await make_group(db, "801", state="verified")
    await give_verdict(db, old)
    await give_verdict(db, recent)
    async with db.transaction() as session:
        await session.execute(
            text(
                "UPDATE verdicts SET validated_at = now() - make_interval(hours => :h)"
                " WHERE claim_group_id = :g"
            ),
            {"h": 49, "g": old},
        )
        await session.execute(
            text(
                "UPDATE verdicts SET validated_at = now() - make_interval(hours => :h)"
                " WHERE claim_group_id = :g"
            ),
            {"h": 47, "g": recent},
        )
    await run_pass(db, settings)
    assert (await group_row(db, old)).state == "passive"
    assert (await group_row(db, recent)).state == "verified"
    assert await events_of(db, names.CLAIM_GROUP_LEVEL_CHANGED) == []  # no event
    assert (await last_runs(db))[0][5]["passive"] == 1


# ---------------------------------------------------------------------------- stats


async def test_run_row_counts_what_the_pass_did(db: Database) -> None:
    settings = make_settings(fc_light_check_share=1.0)
    await make_group(db, "900", level="N2", views=100)
    _, cid = await make_group(db, "901")
    await observe(db, cid, minutes_ago=20, pseudonyms=[pseudo(i) for i in range(5)])
    stale, _ = await make_group(db, "902", level="N2", created_ago=timedelta(days=3))
    await set_below(db, stale, timedelta(hours=30))
    await run_pass(db, settings)
    stats = (await last_runs(db))[0][5]
    assert stats["investigations"] == 1 and stats["light_checks"] == 1
    assert stats["promotions"] == 1 and stats["demotions"] == 1
    assert stats["enqueued"] == 2 and stats["held_back"] == 0
    assert stats["groups"] >= 3
