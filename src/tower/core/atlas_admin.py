"""CLI entry points of the Atlas (``tower atlas …``): seeds and a one-off LEGI sync."""

from __future__ import annotations

import time
from dataclasses import asdict

from tower.core.config import Settings
from tower.core.logging import get_logger

__all__ = ["run_legi_sync", "run_seed"]

log = get_logger(__name__)


async def run_seed(settings: Settings) -> None:
    """Seed the reference news feeds and the country registries (idempotent; rows edited
    by the team are never overwritten)."""
    from tower.core.atlas_seed_feeds import seed_news_feeds
    from tower.core.atlas_seed_registries import seed_country_registries
    from tower.core.db.database import Database

    db = Database(settings)
    try:
        async with db.transaction() as session:
            feeds = await seed_news_feeds(session)
            countries = await seed_country_registries(session)
        print(f"News feeds inserted: {feeds}; country registries: {countries}")
    finally:
        await db.dispose()


async def run_legi_sync(settings: Settings) -> None:
    """Run one LEGI mirror synchronization now (same code as the ``atlas.legi_sync`` job)
    and print its report (archives, articles, bytes, duration, peak RSS)."""
    import resource

    from tower.core.adapters.knowledge.legi_sync import sync_legi
    from tower.core.db.database import Database

    db = Database(settings)
    started = time.monotonic()
    try:
        report = await sync_legi(db, settings)
        peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        print(
            f"LEGI sync: archives={report.archives} up_to_date={report.up_to_date} "
            f"stats={asdict(report.stats)} duration_s={time.monotonic() - started:.1f} "
            f"peak_rss_mb={peak_mb:.0f}"
        )
    finally:
        await db.dispose()
