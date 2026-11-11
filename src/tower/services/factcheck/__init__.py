"""Fact-check service: claim extraction (E2), manual requests, draft assembly (E10) and
translations. See docs/factcheck/pipeline.md.

Only ``tower.core`` is imported. The model router and the platform client are built
lazily by ``tower.core.factcheck.runtime``.
"""

from __future__ import annotations

from tower.core.factcheck import names
from tower.core.services.base import JobSpec, PeriodicTask, Service, Subscription
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
from tower.services.factcheck.periodic import drain_quota, purge_source_cache

service = Service(
    name="factcheck",
    description="Claim extraction, manual requests, draft assembly and translations",
    jobs=(
        JobSpec(kind=names.JOB_EXTRACT, handler=extract, queue=QUEUE, timeout=120),
        JobSpec(kind=names.JOB_REQUEST, handler=request, queue=QUEUE, timeout=60),
        JobSpec(kind=names.JOB_ASSEMBLE, handler=assemble, queue=QUEUE, timeout=90),
        JobSpec(kind=names.JOB_TRANSLATE, handler=translate, queue=QUEUE, timeout=120),
    ),
    subscriptions=(
        Subscription("ingested", (names.CONTENT_INGESTED,), on_content_ingested),
        Subscription("investigated", (names.INVESTIGATION_COMPLETED,), on_investigation_completed),
        Subscription("published", (names.VERDICT_PUBLISHED,), on_verdict_published),
    ),
    periodic=(
        PeriodicTask("factcheck:drain_quota", 60, drain_quota),
        PeriodicTask("factcheck:purge_source_cache", 3600, purge_source_cache),
    ),
)
