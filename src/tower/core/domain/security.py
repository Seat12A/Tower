"""Acteurs, rôles et permissions.

Un acteur est résolu à chaque requête (gate) ou fixé par le processus (system).
Les permissions sont vérifiées dans les cas d'usage, pas seulement dans l'API :
un nouveau client (bot, script) ne peut donc pas les contourner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from tower.core.domain.errors import PermissionDenied


class Permission(StrEnum):
    DOSSIER_READ_PUBLIC = "dossiers:read_public"
    DOSSIER_READ = "dossiers:read"
    DOSSIER_CREATE = "dossiers:create"
    DOSSIER_REVIEW = "dossiers:review"
    DOSSIER_DECIDE = "dossiers:decide"
    DOSSIER_PUBLISH = "dossiers:publish"
    EVENTS_READ = "events:read"
    STAFF_MANAGE = "staff:manage"
    ERRORS_READ = "errors:read"
    ERRORS_MANAGE = "errors:manage"


class Role(StrEnum):
    REVIEWER = "reviewer"  # vérificateur : prend un dossier en revue
    EDITOR = "editor"  # valide, rejette et marque comme publié
    ADMIN = "admin"  # tout, y compris la gestion de l'équipe


class ActorKind(StrEnum):
    SYSTEM = "system"  # processus internes (hangar, radar)
    SERVICE = "service"  # client authentifié par token de service (bot…)
    STAFF = "staff"  # membre de l'équipe, via un service (ID Discord)
    INSTALLATION = "installation"  # extension Chromium (token anonyme)
    ANONYMOUS = "anonymous"  # site public


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.REVIEWER: frozenset(
        {
            Permission.DOSSIER_READ_PUBLIC,
            Permission.DOSSIER_READ,
            Permission.DOSSIER_CREATE,
            Permission.DOSSIER_REVIEW,
        }
    ),
    Role.EDITOR: frozenset(
        {
            Permission.DOSSIER_READ_PUBLIC,
            Permission.DOSSIER_READ,
            Permission.DOSSIER_CREATE,
            Permission.DOSSIER_REVIEW,
            Permission.DOSSIER_DECIDE,
            Permission.DOSSIER_PUBLISH,
        }
    ),
    Role.ADMIN: frozenset(Permission),
}

PUBLIC_PERMISSIONS = frozenset({Permission.DOSSIER_READ_PUBLIC})


@dataclass(frozen=True, slots=True)
class Actor:
    kind: ActorKind
    id: str | None = None
    permissions: frozenset[Permission] = field(default_factory=frozenset)
    via: str | None = None
    """Service par lequel agit un membre de l'équipe (ex. ``intercom``)."""

    @property
    def label(self) -> str:
        """Représentation stable pour les journaux d'audit (``staff:1234@intercom``)."""
        base = f"{self.kind.value}:{self.id}" if self.id else self.kind.value
        return f"{base}@{self.via}" if self.via else base

    @property
    def discord_id(self) -> int | None:
        return int(self.id) if self.kind is ActorKind.STAFF and self.id else None

    def can(self, permission: Permission) -> bool:
        return permission in self.permissions

    def require(self, permission: Permission) -> None:
        if permission not in self.permissions:
            raise PermissionDenied(
                f"Permission requise : {permission.value}", permission=permission.value
            )

    # --- Fabriques --------------------------------------------------------------
    @classmethod
    def system(cls, name: str) -> Actor:
        return cls(ActorKind.SYSTEM, name, frozenset(Permission))

    @classmethod
    def anonymous(cls) -> Actor:
        return cls(ActorKind.ANONYMOUS, None, PUBLIC_PERMISSIONS)

    @classmethod
    def service(cls, name: str, scopes: frozenset[Permission]) -> Actor:
        return cls(ActorKind.SERVICE, name, scopes | PUBLIC_PERMISSIONS)

    @classmethod
    def staff(cls, discord_id: int, roles: frozenset[Role], via: Actor) -> Actor:
        """Membre agissant via un service : droits = rôles ∩ scopes du service."""
        granted: set[Permission] = set()
        for role in roles:
            granted |= ROLE_PERMISSIONS[role]
        return cls(
            ActorKind.STAFF,
            str(discord_id),
            frozenset(granted) & via.permissions | PUBLIC_PERMISSIONS,
            via=via.id,
        )
