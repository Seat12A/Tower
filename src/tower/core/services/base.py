"""Contrat d'un service enfichable (fact-check, modération, ingestion…).

Un service est une valeur déclarative : il ne démarre rien lui-même. Le processus
hangar lit sa déclaration, consomme ses files de jobs et ses abonnements aux
événements. Un service n'importe que ``tower.core`` ; jamais gate, intercom ou un
autre service (ils communiquent par événements et jobs).
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
    """Nom unique du type de job, préfixé par le service : ``factcheck.triage``."""
    handler: JobHandler
    queue: str = "default"
    timeout: float | None = None
    """Durée maximale d'exécution ; doit rester inférieure au bail des jobs."""


@dataclass(frozen=True, slots=True)
class Subscription:
    name: str
    """Nom du consommateur (unique dans le service) ; son curseur est persistant."""
    topics: tuple[str, ...]
    handler: EventHandler


@dataclass(frozen=True, slots=True)
class Service:
    name: str
    description: str = ""
    jobs: tuple[JobSpec, ...] = ()
    subscriptions: tuple[Subscription, ...] = field(default_factory=tuple)
