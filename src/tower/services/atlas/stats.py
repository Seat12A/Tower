"""Statistics reference-fact feed: periodic task ``atlas:stat_feed`` and job
``atlas.stat_refresh`` (queue atlas).

``settings.atlas_stat_indicators`` is a JSON list of tracked indicators (see
``stat_common.StatIndicator``). Each day one job per (indicator, territory) reads the latest
value through the matching adapter and records it as a ``pending`` reference fact; a
reviewer must validate it before it serves. Indicators without explicit territories are
skipped.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.adapters.knowledge.stat_common import (
    StatIndicator,
    parse_stat_indicators,
    period_bounds,
)
from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain import error_codes as ec
from tower.core.domain.errors import DomainError
from tower.core.domain.factcheck.values import FactOrigin
from tower.core.domain.security import Actor
from tower.core.errors import capture
from tower.core.factcheck import names
from tower.core.jobs.queue import PermanentError, RetryLater, enqueue
from tower.core.logging import get_logger
from tower.core.services.base import JobContext
from tower.core.usecases import references as references_uc

if TYPE_CHECKING:
    from tower.core.adapters.knowledge.stat_common import StatSource

__all__ = ["enqueue_stat_refreshes", "set_source_factory", "stat_refresh"]

log = get_logger(__name__)

_RETRY_S = 3600.0

SourceFactory = Callable[[Settings, Database, StatIndicator], Awaitable[Any]]
_factory: SourceFactory | None = None


def set_source_factory(factory: SourceFactory | None) -> None:
    """Replace the adapter builder (tests); ``None`` restores the default."""
    global _factory
    _factory = factory


async def enqueue_stat_refreshes(session: AsyncSession, settings: Settings) -> int:
    """Periodic daily task: one refresh job per (indicator, territory)."""
    today = datetime.now(UTC).date().isoformat()
    count = 0
    for item in parse_stat_indicators(settings.atlas_stat_indicators):
        for territory in item.territories:
            job_id = await enqueue(
                session,
                names.ATLAS_QUEUE,
                names.JOB_STAT_REFRESH,
                {"indicator": item.indicator, "territory": territory},
                dedupe_key=names.stat_key(f"{item.indicator}:{territory}", today),
            )
            count += job_id is not None
    return count


async def _default_source(settings: Settings, db: Database, item: StatIndicator) -> StatSource:
    del db
    kwargs: dict[str, Any] = {
        "indicators": [item],
        "user_agent": settings.http_user_agent,
        "timeout": settings.atlas_http_timeout_s,
    }
    if item.source == "eurostat":
        from tower.core.adapters.knowledge.eurostat import EurostatSource

        return EurostatSource(**kwargs)
    if item.source == "worldbank":
        from tower.core.adapters.knowledge.worldbank import WorldBankSource

        return WorldBankSource(**kwargs)
    if item.source == "oecd":
        from tower.core.adapters.knowledge.oecd import OecdSource

        return OecdSource(**kwargs)
    from tower.core.adapters.knowledge.imf import ImfSource

    return ImfSource(**kwargs)


async def stat_refresh(ctx: JobContext) -> None:
    """Job ``atlas.stat_refresh`` ``{"indicator": str, "territory": str}``: latest value as a
    pending fact (idempotent on ``<source>:<series_key>:<territory>:<period>``)."""
    key = str(ctx.job.payload.get("indicator", ""))
    territory = str(ctx.job.payload.get("territory", "")).upper()
    item = next(
        (
            i
            for i in parse_stat_indicators(ctx.settings.atlas_stat_indicators)
            if i.indicator == key
        ),
        None,
    )
    if item is None or not territory or (item.territories and territory not in item.territories):
        log.info("atlas.stat_refresh_skipped", indicator=key, reason="not_configured")
        return
    try:
        source = await (_factory or _default_source)(ctx.settings, ctx.db, item)
        try:
            value = await source.latest(item.indicator, territory)
        finally:
            closer = getattr(source, "aclose", None)
            if closer is not None:
                await closer()
        if value is None:
            log.info("atlas.stat_refresh_skipped", indicator=key, reason="no_value")
            return
        bounds = period_bounds(value.period)
        if bounds is None:
            log.info("atlas.stat_refresh_skipped", indicator=key, reason="bad_period")
            return
        async with ctx.db.transaction() as session:
            await references_uc.create_reference_fact(
                session,
                Actor.system(names.FACTCHECK_ACTOR),
                indicator=item.indicator,
                label=item.label,
                value=value.value,
                unit=item.unit,
                period_start=bounds[0],
                period_end=bounds[1],
                territory=value.territory,
                source_url=value.source_url,
                source_name=item.source_name,
                published_at=value.published_at or datetime.now(UTC).date(),
                definition_variant=item.definition_variant,
                aliases=item.aliases,
                origin=FactOrigin.STATISTICS,
                external_key=f"{item.source}:{value.series_key}:{value.territory}:{value.period}",
            )
    except DomainError as exc:
        capture(ec.FCK_REFERENCE_FEED_FAILED, exc, indicator=key, territory=territory)
        raise PermanentError(f"invalid reference fact for {key}") from exc
    except Exception as exc:
        capture(ec.FCK_REFERENCE_FEED_FAILED, exc, indicator=key, territory=territory)
        if ctx.job.attempts >= ctx.job.max_attempts:
            raise PermanentError(f"statistics refresh failed for {key}") from exc
        raise RetryLater(_RETRY_S, "statistics refresh failed") from exc
