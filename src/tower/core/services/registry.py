"""Chargement paresseux des services activés (``TOWER_SERVICES``).

Seuls les services listés sont importés : un réplica hangar dédié à la modération ne
charge jamais le code (ni la mémoire) du fact-check.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module

from tower.core.services.base import JobSpec, Service, Subscription

#: Nom public -> ``module:attribut``. Ajouter ici chaque nouveau service.
BUILTIN_SERVICES: dict[str, str] = {
    "ping": "tower.services.ping:service",
    # "factcheck": "tower.services.factcheck:service",
    # "moderation": "tower.services.moderation:service",
}


def load_service(name: str) -> Service:
    try:
        target = BUILTIN_SERVICES[name]
    except KeyError:
        known = ", ".join(sorted(BUILTIN_SERVICES))
        raise ValueError(f"Service inconnu : {name!r} (connus : {known})") from None
    module_name, _, attr = target.partition(":")
    service = getattr(import_module(module_name), attr)
    if not isinstance(service, Service):
        raise TypeError(f"{target} n'est pas un Service")
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
        """Couples (nom de consommateur global, abonnement)."""
        return [(f"{s.name}.{sub.name}", sub) for s in self.services for sub in s.subscriptions]

    def validate(self, *, lease_seconds: float) -> None:
        seen: dict[str, str] = {}
        for service in self.services:
            for spec in service.jobs:
                if spec.kind in seen:
                    raise ValueError(
                        f"Type de job {spec.kind!r} déclaré par {seen[spec.kind]} et {service.name}"
                    )
                seen[spec.kind] = service.name
                if spec.timeout is not None and spec.timeout >= lease_seconds:
                    raise ValueError(
                        f"{spec.kind} : timeout {spec.timeout}s ≥ bail {lease_seconds}s"
                    )


def load_services(names: list[str]) -> ServiceSet:
    unique = list(dict.fromkeys(names))
    return ServiceSet(tuple(load_service(n) for n in unique))
