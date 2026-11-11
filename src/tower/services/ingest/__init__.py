"""Ingest service: links copies of new content, maintains observation partitions,
installation secrets and observation counters. See docs/ingestion.md."""

from __future__ import annotations

from tower.core.services.base import Service

QUEUE = "ingest"

service = Service(name="ingest", description="Content ingestion: copies and maintenance")
