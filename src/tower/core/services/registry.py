"""Lazy loading of the enabled services (``TOWER_SERVICES``).

Only the listed services are imported: a hangar replica dedicated to moderation never
loads the fact-check code (or its memory).
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module

from tower.core.services.base import JobSpec, PeriodicTask, Service, Subscription

#: Public name -> ``module:attribute``. Add each new service here.
BUILTIN_SERVICES: dict[str, str] = {
    "ping": "tower.services.ping:service",
    "ingest": "tower.services.ingest:service",
    # "factcheck": "tower.services.factcheck:service",
    # "moderation": "tower.services.moderation:service",
}


def load_service(name: str) -> Service:
    try:
        target = BUILTIN_SERVICES[name]
    except KeyError:
        known = ", ".join(sorted(BUILTIN_SERVICES))
        raise ValueError(f"Unknown service: {name!r} (known: {known})") from None
    module_name, _, attr = target.partition(":")
    service = getattr(import_module(module_name), attr)
    if not isinstance(service, Service):
        raise TypeError(f"{target} is not a Service")
    return service


@dataclass(frozen=True, slots=True)
class ServiceSet:
    services: tuple[Service, ...]

    @property
    def handlers(self) -> dict[str, JobSpec]:
        return {spec.kind: spec for s in self.services for spec in s.jobs}

    @property
    def queues(self) -> list[str]:
        return sorted({spec.queue for s in self.services for spec in s.jobs})

    @property
    def subscriptions(self) -> list[tuple[str, Subscription]]:
        """Pairs of (global consumer name, subscription)."""
        return [(f"{s.name}.{sub.name}", sub) for s in self.services for sub in s.subscriptions]

    @property
    def periodic(self) -> list[PeriodicTask]:
        return [task for s in self.services for task in s.periodic]

    def validate(self, *, lease_seconds: float) -> None:
        seen: dict[str, str] = {}
        for service in self.services:
            for spec in service.jobs:
                if spec.kind in seen:
                    raise ValueError(
                        f"Job kind {spec.kind!r} declared by both "
                        f"{seen[spec.kind]} and {service.name}"
                    )
                seen[spec.kind] = service.name
                if spec.timeout is not None and spec.timeout >= lease_seconds:
                    raise ValueError(
                        f"{spec.kind}: timeout {spec.timeout}s >= lease {lease_seconds}s"
                    )


def load_services(names: list[str]) -> ServiceSet:
    unique = list(dict.fromkeys(names))
    return ServiceSet(tuple(load_service(n) for n in unique))
