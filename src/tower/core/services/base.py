"""Contract for a pluggable service (fact-check, moderation, ingestion, ...).

A service is a declarative value: it starts nothing itself. The hangar process reads
its declaration, consumes its job queues and its event subscriptions. A service only
imports ``tower.core``; never gate, intercom or another service (they communicate
through events and jobs).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import structlog

from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.events.consumer import EventHandler
from tower.core.jobs.queue import JobRecord


@dataclass(frozen=True, slots=True)
class JobContext:
    job: JobRecord
    db: Database
    settings: Settings
    log: structlog.stdlib.BoundLogger


JobHandler = Callable[[JobContext], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class JobSpec:
    kind: str
    """Unique name of the job type, prefixed by the service: ``factcheck.triage``."""
    handler: JobHandler
    queue: str = "default"
    timeout: float | None = None
    """Maximum execution time; must stay below the job lease."""


@dataclass(frozen=True, slots=True)
class Subscription:
    name: str
    """Consumer name (unique within the service); its cursor is persistent."""
    topics: tuple[str, ...]
    handler: EventHandler


@dataclass(frozen=True, slots=True)
class Service:
    name: str
    description: str = ""
    jobs: tuple[JobSpec, ...] = ()
    subscriptions: tuple[Subscription, ...] = field(default_factory=tuple)
