"""Fact-check service: claim extraction (E2), manual requests, draft assembly (E10) and
translations. See docs/factcheck/pipeline.md.

Only ``tower.core`` is imported. The model router and the platform client are built
lazily by ``tower.core.factcheck.runtime``.
"""

from __future__ import annotations

from tower.core.factcheck import names
from tower.core.services.base import JobSpec, PeriodicTask, Service, Subscription
from tower.services.factcheck.batching import compose_batches, extract_batch
from tower.services.factcheck.jobs import (
    QUEUE,
    assemble,
    extract,
    on_content_ingested,
    on_investigation_completed,
    on_verdict_published,
    request,
    translate,
)
from tower.services.factcheck.light_check import light_check
from tower.services.factcheck.narratives import (
    attach_narrative,
    maintain_narratives,
    narrative_title,
    on_group_created,
)
from tower.services.factcheck.periodic import drain_quota, purge_source_cache
from tower.services.factcheck.reference_feed import enqueue_refreshes, insee_refresh
from tower.services.factcheck.revalidation import expire, revalidate
from tower.services.factcheck.routing import embed_batch, embedding_partitions, on_content_to_embed
from tower.services.factcheck.scheduler import promotion_check, schedule

service = Service(
    name="factcheck",
    description="Claim extraction, manual requests, draft assembly and translations",
    jobs=(
        JobSpec(kind=names.JOB_EXTRACT, handler=extract, queue=QUEUE, timeout=120),
        JobSpec(kind=names.JOB_REQUEST, handler=request, queue=QUEUE, timeout=60),
        JobSpec(kind=names.JOB_ASSEMBLE, handler=assemble, queue=QUEUE, timeout=90),
        JobSpec(kind=names.JOB_TRANSLATE, handler=translate, queue=QUEUE, timeout=120),
        JobSpec(kind=names.JOB_EMBED_BATCH, handler=embed_batch, queue=names.FC_BULK, timeout=240),
        JobSpec(kind=names.JOB_REVALIDATE, handler=revalidate, queue=names.FC_BULK, timeout=120),
        JobSpec(
            kind=names.JOB_INSEE_REFRESH, handler=insee_refresh, queue=names.FC_BULK, timeout=120
        ),
        JobSpec(
            kind=names.JOB_EXTRACT_BATCH, handler=extract_batch, queue=names.FC_BULK, timeout=240
        ),
        JobSpec(kind=names.JOB_LIGHT_CHECK, handler=light_check, queue=names.FC_BULK, timeout=120),
        JobSpec(
            kind=names.JOB_ATTACH_NARRATIVE,
            handler=attach_narrative,
            queue=names.FC_BULK,
            timeout=60,
        ),
        JobSpec(
            kind=names.JOB_NARRATIVE_TITLE, handler=narrative_title, queue=names.FC_BULK, timeout=60
        ),
        JobSpec(
            kind=names.JOB_PROMOTION_CHECK, handler=promotion_check, queue=names.FC_BULK, timeout=60
        ),
    ),
    subscriptions=(
        Subscription("ingested", (names.CONTENT_INGESTED,), on_content_ingested),
        Subscription("embed", (names.CONTENT_INGESTED,), on_content_to_embed),
        Subscription("investigated", (names.INVESTIGATION_COMPLETED,), on_investigation_completed),
        Subscription("published", (names.VERDICT_PUBLISHED,), on_verdict_published),
        Subscription("narratives", (names.CLAIM_GROUP_CREATED,), on_group_created),
    ),
    periodic=(
        PeriodicTask("factcheck:drain_quota", 60, drain_quota),
        PeriodicTask("factcheck:purge_source_cache", 3600, purge_source_cache),
        PeriodicTask("factcheck:embedding_partitions", 3600, embedding_partitions),
        PeriodicTask("factcheck:expire", 600, expire),
        PeriodicTask("factcheck:reference_feed", 86400, enqueue_refreshes),
        PeriodicTask("factcheck:compose_batches", 60, compose_batches),
        PeriodicTask("factcheck:schedule", 60, schedule),
        PeriodicTask("factcheck:narratives", 3600, maintain_narratives),
    ),
)
