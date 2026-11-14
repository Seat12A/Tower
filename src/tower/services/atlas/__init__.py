"""Atlas service (lot 6): synchronizations of the complete Atlas, to run on a DEDICATED
hangar replica (``TOWER_SERVICES=atlas``, queue ``atlas``), never on every replica.

- LEGI mirror: daily diffs (the global dump is imported once with ``tower atlas
  legi-sync``: it does not fit in a job lease);
- reference news feeds: polled every 15 min into ``news_index`` (90-day retention);
- international statistics: daily pending reference facts (Eurostat, World Bank, OECD,
  IMF) for the configured indicators;
- ``controlled_by`` seeding from Wikidata (weekly, never over a team correction).

Only ``tower.core`` is imported. See docs/fact-check.md (Lot 6) and docs/services.md.
"""

from __future__ import annotations

from tower.core.config import get_settings
from tower.core.factcheck import names
from tower.core.services.base import JobSpec, PeriodicTask, Service
from tower.services.atlas.control_seed import control_seed, enqueue_control_seeds
from tower.services.atlas.legal import enqueue_legi_sync, legi_sync
from tower.services.atlas.news import enqueue_news_poll, news_poll, purge_news
from tower.services.atlas.stats import enqueue_stat_refreshes, stat_refresh

service = Service(
    name="atlas",
    description="Atlas synchronizations: LEGI mirror, news index, statistics, registry seeds",
    jobs=(
        JobSpec(kind=names.JOB_LEGI_SYNC, handler=legi_sync, queue=names.ATLAS_QUEUE, timeout=280),
        JobSpec(kind=names.JOB_NEWS_POLL, handler=news_poll, queue=names.ATLAS_QUEUE, timeout=240),
        JobSpec(
            kind=names.JOB_STAT_REFRESH, handler=stat_refresh, queue=names.ATLAS_QUEUE, timeout=60
        ),
        JobSpec(
            kind=names.JOB_CONTROL_SEED, handler=control_seed, queue=names.ATLAS_QUEUE, timeout=60
        ),
    ),
    periodic=(
        PeriodicTask("atlas:legi_sync", 86400, enqueue_legi_sync),
        PeriodicTask("atlas:news_poll", get_settings().atlas_news_poll_s, enqueue_news_poll),
        PeriodicTask("atlas:news_purge", 3600, purge_news),
        PeriodicTask("atlas:stat_feed", 86400, enqueue_stat_refreshes),
        PeriodicTask("atlas:control_seeds", 7 * 86400, enqueue_control_seeds),
    ),
)
