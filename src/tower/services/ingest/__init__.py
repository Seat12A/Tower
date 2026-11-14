"""Ingest service: links copies of new content, checks extension contents against the
public embed, maintains observation partitions, installation secrets and observation
counters. See docs/ingestion.md."""

from __future__ import annotations

from tower.core.services.base import JobSpec, PeriodicTask, Service
from tower.services.ingest.copies import link_copies
from tower.services.ingest.maintenance import maintain_partitions, rotate_secrets
from tower.services.ingest.verify import JOB_VERIFY, schedule_verification, verify_batch

QUEUE = "ingest"

service = Service(
    name="ingest",
    description="Content ingestion: copies, verification and maintenance",
    jobs=(
        JobSpec(kind="ingest.link_copies", handler=link_copies, queue=QUEUE, timeout=60),
        JobSpec(kind=JOB_VERIFY, handler=verify_batch, queue=QUEUE, timeout=110),
    ),
    periodic=(
        PeriodicTask("ingest:verify", 60, schedule_verification),
        PeriodicTask("ingest:partitions", 3600, maintain_partitions),
        PeriodicTask("ingest:secrets", 600, rotate_secrets),
    ),
)
