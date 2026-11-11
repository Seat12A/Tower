"""Adaptive scheduling: which watched accounts to read now (singleton periodic task)."""

from __future__ import annotations

import math
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.config import Settings
from tower.core.domain.llm import month_period
from tower.core.domain.radar import IntervalBounds, base_interval, daily_read_allowance
from tower.core.jobs.queue import enqueue
from tower.core.logging import get_logger
from tower.services.radar.client import SUPPORTED_PLATFORMS

log = get_logger(__name__)

QUEUE = "radar"
POLL_KIND = "radar.poll_account"

_ENSURE_COUNTER = text(
    """
    INSERT INTO budget_counters (scope, period_start, period_end, cap_usd_micros)
    SELECT :scope, CAST(:ps AS date), CAST(:pe AS date),
           coalesce(
               (SELECT cap_usd_micros FROM budget_counters
                WHERE scope = :scope AND period_start < CAST(:ps AS date)
                ORDER BY period_start DESC LIMIT 1),
               :default_cap)
    ON CONFLICT (scope, period_start) DO NOTHING
    """
)
_COUNTER = text(
    """
    SELECT cap_usd_micros - spent_usd_micros - reserved_usd_micros
    FROM budget_counters WHERE scope = :scope AND period_start = CAST(:ps AS date)
    """
)
_POLLS_TODAY = text(
    """
    SELECT count(*) FROM jobs
    WHERE kind = 'radar.poll_account' AND created_at >= CAST(:day AS timestamptz)
    """
)
# Priority and timing only: never the author or anything political (rule 10).
_DUE = text(
    """
    SELECT w.public_id, w.priority, w.poll_interval_s
    FROM watched_accounts w JOIN platform_accounts a ON a.id = w.account_id
    WHERE w.active AND a.platform = :platform AND a.external_id IS NOT NULL
      AND (w.next_poll_at IS NULL OR w.next_poll_at <= CAST(:now AS timestamptz))
    ORDER BY w.priority DESC, w.next_poll_at NULLS FIRST, w.id
    LIMIT :limit
    """
)
# Scheduling columns are written in plain SQL: never through the ORM (``version_id_col``).
_LEASE = text(
    """
    UPDATE watched_accounts
    SET next_poll_at = CAST(:now AS timestamptz) + make_interval(secs => :secs)
    WHERE public_id = :public_id
    """
)


def bounds_from_settings(settings: Settings) -> IntervalBounds:
    return IntervalBounds(
        high_s=settings.radar_interval_high_s,
        normal_s=settings.radar_interval_normal_s,
        low_s=settings.radar_interval_low_s,
        min_s=settings.radar_interval_min_s,
        max_s=settings.radar_interval_max_s,
    )


async def schedule_polls(session: AsyncSession, settings: Settings) -> int:
    """Enqueue due polls within the daily allowance; return how many were enqueued."""
    now = (await session.execute(text("SELECT now()"))).scalar_one().astimezone(UTC)
    bounds = bounds_from_settings(settings)
    day_start = datetime(now.year, now.month, now.day, tzinfo=UTC)
    period_start, period_end = month_period(now)
    post_price = round(settings.x_price_post_read_usd * 1e6)
    total = 0
    for platform in SUPPORTED_PLATFORMS:
        scope = f"provider:{platform}"
        params = {"scope": scope, "ps": period_start}
        await session.execute(
            _ENSURE_COUNTER,
            {
                **params,
                "pe": period_end,
                "default_cap": round(settings.x_monthly_cap_usd * 1e6),
            },
        )
        remaining = int((await session.execute(_COUNTER, params)).scalar_one())
        allowance = daily_read_allowance(
            remaining_usd_micros=remaining,
            days_left=(period_end - now.date()).days,
            avg_read_cost_usd_micros=math.ceil(settings.radar_expected_posts_per_poll * post_price),
        )
        limit = settings.radar_schedule_batch
        if allowance is not None:
            today = int((await session.execute(_POLLS_TODAY, {"day": day_start})).scalar_one())
            limit = min(max(allowance - today, 0), limit)
        if limit <= 0:
            log.info("radar.schedule_capped", platform=platform, remaining_usd_micros=remaining)
            continue
        due = (
            await session.execute(_DUE, {"platform": platform, "now": now, "limit": limit})
        ).all()
        for public_id, priority, interval in due:
            job_id = await enqueue(
                session,
                QUEUE,
                POLL_KIND,
                {"watched": str(public_id)},
                dedupe_key=f"radar:poll:{public_id}",
            )
            secs = int(interval) if interval is not None else base_interval(int(priority), bounds)
            await session.execute(_LEASE, {"now": now, "secs": secs, "public_id": public_id})
            if job_id is not None:
                total += 1
    return total
