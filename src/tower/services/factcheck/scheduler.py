"""E4 scheduler (lot 5): adaptive passes, group metrics, promotions and demotions, waves,
priority and enqueueing of investigations and light checks.

``schedule`` ticks every minute and does a pass only when the adaptive interval has
elapsed. A pass is one transaction (singleton periodic task). Nothing here reads the author
of a post, its account or its political side: the priority comes from the observations of
the group, the platform metrics, the theme, the verifiability and the narrative wave state.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.budget import BudgetRefused, reserve_budget, settle_budget
from tower.core.config import Settings
from tower.core.domain import error_codes as ec
from tower.core.domain.factcheck import scheduling as rules
from tower.core.domain.llm import month_period
from tower.core.domain.security import Actor
from tower.core.errors import capture
from tower.core.factcheck import names, runtime
from tower.core.factcheck.profiles import active_profile
from tower.core.jobs.queue import PermanentError, enqueue
from tower.core.logging import get_logger
from tower.core.models.factcheck import ContentClaim
from tower.core.models.factcheck_v2 import SchedulerRun
from tower.core.models.ingestion import ContentItem
from tower.core.ports.platforms import (
    PlatformError,
    PlatformNotConfigured,
    UnsupportedOperation,
)
from tower.core.services.base import JobContext
from tower.core.usecases import scheduling as uc
from tower.services.factcheck import assembly as asm

__all__ = ["promotion_check", "schedule"]

log = get_logger(__name__)

_ACTOR = names.FACTCHECK_ACTOR
_MAX_GROUPS = 5000
_ACTIVE_WINDOW = timedelta(hours=48)
_COST_FLOOR_MICROS = 10_000  # 1 cent
_MICROS_PER_CENT = 10_000
_PASSIVE_AFTER = timedelta(hours=48)
_VIEW_KEYS = ("impression_count", "views")
_FIRST_PASS_LOOKBACK = timedelta(hours=24)

# SQL aggregates only: no observation row is loaded.
_METRICS_SQL = text(
    """
    SELECT g.id AS group_id,
           count(DISTINCT o.installation_pseudonym)
               FILTER (WHERE o.installation_pseudonym IS NOT NULL) AS observers,
           count(DISTINCT o.id) FILTER (WHERE o.observed_at >= :t1) AS last_hour,
           count(DISTINCT o.id)
               FILTER (WHERE o.observed_at >= :t2 AND o.observed_at < :t1) AS prev_hour,
           coalesce(max(greatest(
               CASE WHEN c.metrics->>'impression_count' ~ '^[0-9]{1,15}$'
                    THEN (c.metrics->>'impression_count')::bigint ELSE 0 END,
               CASE WHEN c.metrics->>'views' ~ '^[0-9]{1,15}$'
                    THEN (c.metrics->>'views')::bigint ELSE 0 END)), 0) AS views
    FROM claim_groups g
    JOIN claim_group_members m ON m.group_id = g.id AND m.removed_at IS NULL
    JOIN content_claims cc ON cc.claim_id = m.claim_id
    JOIN content_items c ON c.id = cc.content_id
    LEFT JOIN observations o ON o.content_id = c.id AND o.observed_at >= :t6
    WHERE g.state NOT IN ('merged', 'passive') AND g.updated_at >= :t48
    GROUP BY g.id
    LIMIT :limit
    """
)

_GROUPS_SQL = text(
    """
    SELECT g.id AS id, g.level AS level, g.state AS state, g.light_signal AS light_signal,
           g.below_threshold_since AS below_since, g.reach AS views,
           g.observers_6h AS observers, g.velocity AS velocity,
           g.active_verdict_id IS NOT NULL AS has_verdict,
           g.created_at AS created_at, g.light_checked_at AS light_checked_at,
           g.representative_claim_id AS claim_id,
           c.theme AS theme, c.verifiability AS verifiability,
           (SELECT n.state FROM narrative_groups ng
              JOIN narratives n ON n.id = ng.narrative_id
             WHERE ng.group_id = g.id AND ng.removed_at IS NULL LIMIT 1) AS narrative_state,
           EXISTS (SELECT 1 FROM investigations i WHERE i.claim_group_id = g.id) AS investigated,
           EXISTS (SELECT 1 FROM investigations i WHERE i.claim_group_id = g.id
                    AND i.stage NOT IN ('completed', 'assembled', 'failed')) AS running
    FROM claim_groups g
    JOIN claims c ON c.id = g.representative_claim_id
    WHERE g.state NOT IN ('merged', 'passive') AND g.level IN ('N1', 'N2')
      AND g.updated_at >= :t48
    ORDER BY g.id
    LIMIT :limit
    """
)


@dataclass(frozen=True, slots=True)
class _Group:
    id: int
    level: str
    state: str
    light_signal: str | None
    below_since: datetime | None
    views: int
    observers: int
    velocity: float
    has_verdict: bool
    created_at: datetime
    light_checked_at: datetime | None
    claim_id: int
    theme: str | None
    verifiability: float
    narrative_state: str | None
    investigated: bool
    running: bool

    @property
    def in_wave(self) -> bool:
        return self.narrative_state in ("surge", "alert")

    def metrics(self) -> rules.GroupMetrics:
        return rules.GroupMetrics(
            observers_6h=self.observers,
            velocity=self.velocity,
            views=self.views,
            reach=rules.reach(self.views, self.observers, self.velocity),
            light_signal=self.light_signal,
            narrative_state=self.narrative_state,
        )


def _actor() -> Actor:
    return Actor.system(_ACTOR)


async def _rate_per_hour(session: AsyncSession, now: datetime) -> float:
    count = await session.scalar(
        select(func.count())
        .select_from(ContentItem)
        .where(ContentItem.first_seen_at >= now - timedelta(hours=1))
    )
    return float(count or 0)


async def _group_metrics(session: AsyncSession, now: datetime) -> list[uc.GroupMetricsRow]:
    result = await session.execute(
        _METRICS_SQL,
        {
            "t1": now - timedelta(hours=1),
            "t2": now - timedelta(hours=2),
            "t6": now - timedelta(hours=6),
            "t48": now - _ACTIVE_WINDOW,
            "limit": _MAX_GROUPS,
        },
    )
    rows: list[uc.GroupMetricsRow] = []
    for row in result.mappings():
        last_hour, previous = int(row["last_hour"]), int(row["prev_hour"])
        velocity = last_hour / max(previous, 1) if last_hour >= 10 else 0.0
        rows.append(
            uc.GroupMetricsRow(
                group_id=int(row["group_id"]),
                observers_6h=int(row["observers"]),
                velocity=velocity,
                views=int(row["views"]),
            )
        )
    return rows


async def _load_groups(session: AsyncSession, now: datetime) -> list[_Group]:
    result = await session.execute(_GROUPS_SQL, {"t48": now - _ACTIVE_WINDOW, "limit": _MAX_GROUPS})
    return [
        _Group(
            id=int(r["id"]),
            level=r["level"],
            state=r["state"],
            light_signal=r["light_signal"],
            below_since=r["below_since"],
            views=int(r["views"] or 0),
            observers=int(r["observers"] or 0),
            velocity=float(r["velocity"] or 0.0),
            has_verdict=bool(r["has_verdict"]),
            created_at=r["created_at"],
            light_checked_at=r["light_checked_at"],
            claim_id=int(r["claim_id"]),
            theme=r["theme"],
            verifiability=float(r["verifiability"] or 0.0),
            narrative_state=r["narrative_state"],
            investigated=bool(r["investigated"]),
            running=bool(r["running"]),
        )
        for r in result.mappings()
    ]


async def _level_costs(session: AsyncSession, now: datetime) -> dict[str, float]:
    """Mean cost of the finished investigations of each level over 30 days, in micros."""
    result = await session.execute(
        text(
            "SELECT level, avg(cost_usd_micros) FROM investigations"
            " WHERE created_at >= CAST(:since AS timestamptz)"
            " AND stage IN ('completed', 'assembled') GROUP BY level"
        ),
        {"since": now - timedelta(days=30)},
    )
    return {str(level): float(mean or 0.0) for level, mean in result.all()}


async def _promotions_and_demotions(
    session: AsyncSession,
    actor: Actor,
    settings: Settings,
    groups: list[_Group],
    now: datetime,
) -> tuple[int, int]:
    n1_reaches = [g.metrics().reach for g in groups if g.level == "N1"]
    thresholds = rules.PromotionThresholds(
        observers=settings.fc_promotion_observers,
        velocity_factor=settings.fc_promotion_velocity_factor,
        views=settings.fc_promotion_views,
        median_reach=statistics.median(n1_reaches) if n1_reaches else 0.0,
    )
    hour = now.strftime("%Y%m%d%H")
    promotions = 0
    below: list[int] = []
    clear: list[int] = []
    demote: list[int] = []
    for group in groups:
        reasons = rules.promotion_reasons(group.metrics(), thresholds)
        if group.level == "N1" and reasons:
            job_id = await enqueue(
                session,
                names.FC_BULK,
                names.JOB_PROMOTION_CHECK,
                {"group_id": group.id, "reasons": reasons},
                dedupe_key=names.promotion_key(group.id, hour),
            )
            promotions += job_id is not None
        elif group.level == "N2":
            if reasons:
                clear.append(group.id)
                continue
            if group.below_since is None:
                below.append(group.id)
            elif rules.demotion_due(
                group.below_since,
                now=now,
                hours=settings.fc_demotion_hours,
                investigating=group.running or group.state in ("queued", "investigating"),
            ):
                demote.append(group.id)
    await uc.set_below_threshold(session, actor, below=below, clear=clear, now=now)
    demotions = 0
    for group_id in demote:
        demotions += await uc.change_level(
            session, actor, group_id, "N1", reason="demotion", now=now
        )
    return promotions, demotions


def _priorities(groups: list[_Group], costs: dict[str, float]) -> dict[int, float]:
    values: dict[int, float] = {}
    for group in groups:
        mean = costs.get(group.level, 0.0)
        cost_cents = max(mean, _COST_FLOOR_MICROS) / _MICROS_PER_CENT
        values[group.id] = rules.priority(
            rules.PriorityInput(
                views=group.views,
                observers=group.observers,
                velocity=group.velocity,
                theme=group.theme,
                verifiability=group.verifiability,
                revalidation=group.has_verdict,
                narrative_state=group.narrative_state,
                level_cost_cents=cost_cents,
            )
        )
    return values


async def _n2_budget_remaining(session: AsyncSession, now: datetime) -> int | None:
    """Remaining cap of the ``level:N2`` counter of the current period (None: uncapped)."""
    start, _ = month_period(now)
    row = (
        await session.execute(
            text(
                "SELECT cap_usd_micros - spent_usd_micros - reserved_usd_micros FROM"
                " budget_counters WHERE scope = :scope AND period_start = :start"
            ),
            {"scope": names.level_scope("N2"), "start": start},
        )
    ).first()
    return None if row is None else int(row[0])


async def _deep_backlog(session: AsyncSession) -> int:
    return int(
        await session.scalar(
            text("SELECT count(*) FROM jobs WHERE queue = :q AND status IN ('queued', 'running')"),
            {"q": names.FC_DEEP},
        )
        or 0
    )


async def _first_content(session: AsyncSession, claim_id: int) -> ContentItem | None:
    return await session.scalar(
        select(ContentItem)
        .join(ContentClaim, ContentClaim.content_id == ContentItem.id)
        .where(ContentClaim.claim_id == claim_id)
        .order_by(ContentItem.id)
        .limit(1)
    )


async def _enqueue_investigations(
    session: AsyncSession,
    actor: Actor,
    settings: Settings,
    candidates: list[_Group],
    priorities: dict[int, float],
    now: datetime,
) -> tuple[int, dict[str, int]]:
    """N2 groups in priority order, within the level budget, the daily draft quota and the
    capacity of ``fc-deep``. Returns the number requested and the held-back counts."""
    held = {"budget": 0, "quota": 0, "capacity": 0}
    if not candidates:
        return 0, held
    await asm.lock_quota(session)
    profile = await active_profile(session, settings)
    slots = profile.draft_daily_quota - await asm.today_count(session, now)
    backlog = await _deep_backlog(session)
    remaining = await _n2_budget_remaining(session, now)
    costs = await _level_costs(session, now)
    unit_cost = int(max(costs.get("N2", 0.0), _COST_FLOOR_MICROS))
    requested = 0
    for group in sorted(candidates, key=lambda g: (-priorities[g.id], g.id)):
        if slots <= requested:
            held["quota"] += 1
        elif backlog + requested >= settings.fc_deep_queue_capacity:
            held["capacity"] += 1
        elif remaining is not None and remaining < unit_cost:
            held["budget"] += 1
        else:
            content = await _first_content(session, group.claim_id)
            if content is None:
                continue
            investigation = await uc.schedule_investigation(
                session,
                actor,
                group_id=group.id,
                content=content,
                flags={"scheduled": True},
            )
            if investigation is None:
                continue
            requested += 1
            if remaining is not None:
                remaining -= unit_cost
    if held["capacity"]:
        capture(
            ec.FCK_SCHEDULER_BACKPRESSURE,
            message="Scheduler held investigations back: fc-deep at capacity",
            held_back=held["capacity"],
            backlog=backlog,
            capacity=settings.fc_deep_queue_capacity,
        )
    return requested, held


async def _enqueue_light_checks(
    session: AsyncSession,
    settings: Settings,
    candidates: list[_Group],
    priorities: dict[int, float],
) -> int:
    if not candidates or settings.fc_light_check_share <= 0:
        return 0
    version = (await active_profile(session, settings)).version
    ranked = sorted(candidates, key=lambda g: (-priorities[g.id], g.id))
    top = ranked[: math.ceil(settings.fc_light_check_share * len(ranked))]
    enqueued = 0
    for group in top:
        job_id = await enqueue(
            session,
            names.FC_BULK,
            names.JOB_LIGHT_CHECK,
            {"group_id": group.id, "dossier_investigation_id": None},
            dedupe_key=names.light_check_key(group.id, version),
        )
        enqueued += job_id is not None
    return enqueued


async def schedule(session: AsyncSession, settings: Settings) -> int:
    """Periodic singleton ``factcheck:schedule`` (ticks every minute; does a pass when the
    adaptive interval has elapsed)."""
    now = datetime.now(UTC)
    actor = _actor()
    rate = await _rate_per_hour(session, now)
    interval = rules.interval_s(
        rate,
        target=settings.fc_scheduler_target_per_pass,
        min_s=settings.fc_scheduler_min_interval_s,
        max_s=settings.fc_scheduler_max_interval_s,
    )
    last = await session.scalar(select(func.max(SchedulerRun.started_at)))
    if last is not None and (now - last).total_seconds() < interval:
        return 0
    run = await uc.open_run(session, actor, now=now, rate_per_hour=rate, interval_s=interval)

    # 1. Metrics of the active groups.
    metric_rows = await _group_metrics(session, now)
    await uc.apply_group_metrics(session, actor, metric_rows)
    groups = await _load_groups(session, now)

    # 2. Promotions (confirmed later by a job) and demotions.
    promotions, demotions = await _promotions_and_demotions(session, actor, settings, groups, now)

    # 3. Waves (narratives, owned by another module; lazy import).
    from tower.services.factcheck import narratives

    waves = await narratives.waves_step(session, settings, now)

    # Verified groups whose verdict is old enough only need passive watching.
    passive = await uc.mark_passive(session, actor, cutoff=now - _PASSIVE_AFTER)

    # 4. Priority of the candidates (state read again: waves and demotions moved some).
    groups = await _load_groups(session, now)
    since = last if last is not None else now - _FIRST_PASS_LOOKBACK
    n2 = [
        g
        for g in groups
        if g.level == "N2" and g.state in ("dormant", "queued") and not g.investigated
    ]
    n1_new = [
        g
        for g in groups
        if g.level == "N1"
        and g.state == "dormant"
        and g.light_checked_at is None
        and g.created_at >= since
    ]
    priorities = _priorities(n2 + n1_new, await _level_costs(session, now))
    await uc.set_priorities(session, actor, priorities)

    # 5. Enqueue; groups of a surging narrative are handled by the waves step.
    n2 = [g for g in n2 if not g.in_wave]
    n1_new = [g for g in n1_new if not g.in_wave]
    investigations, held = await _enqueue_investigations(
        session, actor, settings, n2, priorities, now
    )
    light_checks = await _enqueue_light_checks(session, settings, n1_new, priorities)

    stats: dict[str, Any] = {
        "rate": rate,
        "interval_s": interval,
        "groups": len(groups),
        "promotions": promotions,
        "demotions": demotions,
        "passive": passive,
        "enqueued": investigations + light_checks,
        "investigations": investigations,
        "light_checks": light_checks,
        "held_back": sum(held.values()),
        "held_back_by": held,
        "waves": waves,
    }
    await uc.finish_run(session, actor, run, stats, now=datetime.now(UTC))
    log.info("scheduler.pass", **{k: v for k, v in stats.items() if k != "waves"})
    return promotions + demotions + passive + investigations + light_checks


# --------------------------------------------------------------------- promotion check


def _fresh_views(values: dict[str, int] | Any) -> int:
    for key in _VIEW_KEYS:
        value = values.get(key)
        if isinstance(value, int | float):
            return int(value)
    return 0


async def promotion_check(ctx: JobContext) -> None:
    """Job ``factcheck.promotion_check``: platform read confirming N2 metrics."""
    payload = ctx.job.payload
    try:
        group_id = int(payload["group_id"])
        reasons = [str(r) for r in payload.get("reasons") or []]
    except (KeyError, TypeError, ValueError) as exc:
        raise PermanentError("bad promotion_check payload") from exc
    if not reasons:
        raise PermanentError("promotion_check without reasons")
    settings = ctx.settings
    actor = _actor()
    thresholds = rules.PromotionThresholds(
        observers=settings.fc_promotion_observers,
        velocity_factor=settings.fc_promotion_velocity_factor,
        views=settings.fc_promotion_views,
        median_reach=0.0,
    )

    async with ctx.db.session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT g.level, g.state, ci.platform, ci.external_id"
                    " FROM claim_groups g"
                    " LEFT JOIN LATERAL (SELECT c.platform, c.external_id FROM content_claims cc"
                    "   JOIN content_items c ON c.id = cc.content_id"
                    "   WHERE cc.claim_id = g.representative_claim_id ORDER BY c.id LIMIT 1) ci"
                    "   ON true WHERE g.id = :id"
                ),
                {"id": group_id},
            )
        ).first()
    if row is None or row[0] != "N1" or row[1] == "merged":
        log.info("promotion.skipped", group=group_id)
        return
    platform, external_id = row[2], row[3]

    unconfirmed = False
    client = None
    if platform is not None:
        try:
            client = runtime.get_platform_client(settings, str(platform))
        except PlatformNotConfigured:
            client = None
    if client is None or external_id is None:
        unconfirmed = True
    else:
        try:
            holds = await reserve_budget(
                ctx.db,
                (f"provider:{platform}",),
                client.cost_per_read_usd_micros,
                ref=f"factcheck:promotion:{group_id}",
                now=datetime.now(UTC),
                ttl_s=settings.budget_reservation_ttl_s,
            )
        except BudgetRefused as exc:
            capture(
                ec.FCK_PROMOTION_UNCONFIRMED,
                exc,
                group=group_id,
                cause="budget",
            )
            return
        cost = 0
        metrics = None
        try:
            metrics = await client.get_metrics(str(external_id))
            if metrics is not None:
                cost = client.cost_per_read_usd_micros
        except UnsupportedOperation:
            unconfirmed = True
        except PlatformError as exc:
            capture(ec.FCK_PROMOTION_UNCONFIRMED, exc, group=group_id, cause="platform_error")
            return
        finally:
            async with ctx.db.transaction() as session:
                await settle_budget(session, holds, cost, actor=_ACTOR)
        if not unconfirmed:
            if metrics is None:
                capture(
                    ec.FCK_PROMOTION_UNCONFIRMED,
                    message="Representative post not found by the platform",
                    group=group_id,
                    cause="post_missing",
                )
                return
            if not rules.promotion_confirmed(reasons, _fresh_views(metrics.values), thresholds):
                capture(
                    ec.FCK_PROMOTION_UNCONFIRMED,
                    message="Fresh platform metrics are below the promotion threshold",
                    group=group_id,
                    cause="below_threshold",
                    reasons=reasons,
                )
                return

    async with ctx.db.transaction() as session:
        changed = await uc.change_level(
            session,
            actor,
            group_id,
            "N2",
            reason="promotion",
            now=datetime.now(UTC),
            unconfirmed=unconfirmed,
        )
    log.info("promotion.done", group=group_id, changed=changed, unconfirmed=unconfirmed)
