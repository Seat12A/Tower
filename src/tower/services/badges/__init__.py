"""Badge service: Wikidata import, X government verification, affiliation expiry.

Jobs (queue ``badges``):
- ``badges.wikidata_scope``: lists the (item, X id) pairs of a scope and enqueues one
  ``badges.wikidata_batch`` job per 200 pairs;
- ``badges.wikidata_batch``: reads types, occupations, label, logo and affiliation
  links of the batch, then applies them (badges, pending affiliations);
- ``badges.x_verify``: checks up to 100 X accounts (``verified_type``) within the daily
  cap and the ``provider:x`` budget.
Periodic (singletons): ``badges:schedule`` (imports due, X checks due) and
``badges:expire`` (affiliations past one year, stale Wikidata badges). See docs/badges.md.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import orjson
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.budget import BudgetRefused, reserve_budget, settle_budget
from tower.core.config import Settings
from tower.core.domain import error_codes as ec
from tower.core.domain.badges import X_GOVERNMENT_VERIFIED_TYPE
from tower.core.domain.security import Actor
from tower.core.errors import capture
from tower.core.jobs.queue import RetryLater, enqueue
from tower.core.ports.knowledge import SourceUnavailable
from tower.core.ports.platforms import PlatformError, PlatformNotConfigured, PlatformRateLimited
from tower.core.services.base import JobContext, JobSpec, PeriodicTask, Service
from tower.core.usecases import badge_sources as src
from tower.core.usecases.badge_affiliations import expire_affiliations
from tower.services.badges import clients

QUEUE = src.QUEUE
ACTOR_NAME = "badges"
X_BATCH = 100
_RETRY_S = 600.0


def _actor() -> Actor:
    return Actor.system(ACTOR_NAME)


def _day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def wikidata_scope(ctx: JobContext) -> None:
    scope = str(ctx.job.payload.get("scope", ""))
    source = clients.wikidata_source(ctx.settings)
    try:
        accounts = await source.x_accounts(scope)
    except SourceUnavailable as exc:
        capture(ec.BDG_WIKIDATA_FAILED, exc, scope=scope, step="list")
        raise RetryLater(_RETRY_S, "wikidata unavailable") from exc
    triples, skipped = src.batch_pairs(accounts)
    chunks = [triples[i : i + src.BATCH_SIZE] for i in range(0, len(triples), src.BATCH_SIZE)]
    now = datetime.now(UTC)
    async with ctx.db.transaction() as session:
        for chunk in chunks:
            digest = hashlib.sha1(orjson.dumps(chunk), usedforsecurity=False).hexdigest()[:16]
            await enqueue(
                session,
                QUEUE,
                src.BATCH_JOB,
                {"scope": scope, "items": chunk},
                dedupe_key=f"badges:wdb:{scope}:{digest}",
            )
        await src.record_listing(
            session, scope, accounts=len(triples), batches=len(chunks), now=now
        )
    ctx.log.info(
        "badges.wikidata_listed",
        scope=scope,
        accounts=len(triples),
        skipped=skipped,
        batches=len(chunks),
    )


async def wikidata_batch(ctx: JobContext) -> None:
    raw = ctx.job.payload.get("items") or []
    triples = [
        [str(t[0]), str(t[1]), str(t[2]) if len(t) > 2 else ""]
        for t in raw
        if isinstance(t, list) and len(t) >= 2
    ]
    if not triples:
        return
    source = clients.wikidata_source(ctx.settings)
    try:
        items = await source.items([t[0] for t in triples])
    except SourceUnavailable as exc:
        capture(ec.BDG_WIKIDATA_FAILED, exc, scope=ctx.job.payload.get("scope"), step="items")
        raise RetryLater(_RETRY_S, "wikidata unavailable") from exc
    async with ctx.db.transaction() as session:
        stats = await src.apply_wikidata_batch(session, _actor(), triples, items)
    ctx.log.info("badges.wikidata_batch", scope=ctx.job.payload.get("scope"), **stats)


async def x_verify(ctx: JobContext) -> None:
    settings = ctx.settings
    now = datetime.now(UTC)
    async with ctx.db.session() as session:
        done_today = await src.x_checks_since(session, _day_start(now))
        limit = min(X_BATCH, settings.badges_x_daily_checks - done_today)
        due = await src.due_x_checks(
            session, limit=limit, recheck_days=settings.badges_x_recheck_days, now=now
        )
    if not due:
        return
    try:
        client = clients.x_client(settings)
    except PlatformNotConfigured:
        ctx.log.warning("badges.x_not_configured", due=len(due))
        return
    unit = client.cost_per_account_read_usd_micros
    try:
        holds = await reserve_budget(
            ctx.db,
            ("provider:x",),
            unit * len(due),
            ref=f"badges:x:{ctx.job.id}",
            now=now,
            ttl_s=settings.budget_reservation_ttl_s,
        )
    except BudgetRefused as exc:
        ctx.log.warning("badges.x_budget_exhausted", scope=exc.scope)
        raise RetryLater(max(_RETRY_S, (exc.retry_at - now).total_seconds())) from exc
    try:
        accounts = await client.get_accounts(due)
    except PlatformError as exc:
        async with ctx.db.transaction() as session:
            await settle_budget(session, holds, 0, actor=f"system:{ACTOR_NAME}")
        if isinstance(exc, PlatformRateLimited):
            raise RetryLater(max(60.0, exc.retry_after), "x rate limited") from exc
        capture(ec.BDG_X_CHECK_FAILED, exc, status=exc.status, accounts=len(due))
        raise RetryLater(_RETRY_S, "x unavailable") from exc
    async with ctx.db.transaction() as session:
        await settle_budget(session, holds, unit * len(accounts), actor=f"system:{ACTOR_NAME}")
        stats = await src.apply_x_results(
            session, _actor(), due, accounts, government_type=X_GOVERNMENT_VERIFIED_TYPE
        )
        if len(due) == X_BATCH and limit > len(due):
            await enqueue(session, QUEUE, src.X_JOB, {}, dedupe_key="badges:x:next")
    ctx.log.info("badges.x_verified", **stats)


async def schedule(session: AsyncSession, settings: Settings) -> int:
    """Enqueue the imports due and, if any account waits, an X verification."""
    now = datetime.now(UTC)
    scopes = await src.scopes_due(
        session,
        settings.badges_wikidata_scope_list,
        interval=timedelta(seconds=settings.badges_wikidata_interval_s),
        now=now,
    )
    count = 0
    for scope in scopes:
        if await enqueue(
            session, QUEUE, src.SCOPE_JOB, {"scope": scope}, dedupe_key=f"badges:wd:{scope}"
        ):
            count += 1
    if settings.badges_x_daily_checks > 0:
        due = await src.due_x_checks(
            session, limit=1, recheck_days=settings.badges_x_recheck_days, now=now
        )
        if due and await enqueue(session, QUEUE, src.X_JOB, {}, dedupe_key="badges:x"):
            count += 1
    return count


async def expire(session: AsyncSession, settings: Settings) -> int:
    actor = _actor()
    expired = await expire_affiliations(session, actor)
    stale = await src.revoke_stale_wikidata(
        session,
        actor,
        stale_days=settings.badges_wikidata_stale_days,
        fresh_window=timedelta(seconds=settings.badges_wikidata_interval_s) + timedelta(days=1),
    )
    return expired + stale


service = Service(
    name="badges",
    description="Verification badges: Wikidata import, X government checks, expiry",
    jobs=(
        JobSpec(kind=src.SCOPE_JOB, handler=wikidata_scope, queue=QUEUE, timeout=240),
        JobSpec(kind=src.BATCH_JOB, handler=wikidata_batch, queue=QUEUE, timeout=120),
        JobSpec(kind=src.X_JOB, handler=x_verify, queue=QUEUE, timeout=60),
    ),
    periodic=(
        PeriodicTask(name="badges:schedule", every_seconds=3600, fn=schedule),
        PeriodicTask(name="badges:expire", every_seconds=3600, fn=expire),
    ),
)
