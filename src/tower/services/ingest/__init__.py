"""Ingest service: links copies of new content, maintains observation partitions,
installation secrets and observation counters. See docs/ingestion.md."""

from __future__ import annotations

from tower.core.services.base import JobSpec, PeriodicTask, Service
from tower.services.ingest.copies import link_copies
from tower.services.ingest.maintenance import maintain_partitions, rotate_secrets

QUEUE = "ingest"

service = Service(
    name="ingest",
    description="Content ingestion: copies and maintenance",
    jobs=(JobSpec(kind="ingest.link_copies", handler=link_copies, queue=QUEUE, timeout=60),),
    periodic=(
        PeriodicTask("ingest:partitions", 3600, maintain_partitions),
        PeriodicTask("ingest:secrets", 600, rotate_secrets),
    ),
)
