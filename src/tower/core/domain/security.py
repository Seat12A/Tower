"""Actors, roles and permissions.

An actor is resolved on each request (gate) or set by the process (system).
Permissions are checked in the use cases, not only in the API: a new client
(bot, script) therefore cannot bypass them.
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
    CONTENT_INGEST = "content:ingest"
    WATCHLIST_READ = "watchlist:read"
    WATCHLIST_WRITE = "watchlist:write"
    USAGE_READ = "usage:read"
    FACTCHECK_REQUEST = "factcheck:request"
    GROUPS_MANAGE = "claim_groups:manage"  # merge, split (lot 4)
    FACTS_WRITE = "reference_facts:write"  # create a reference fact
    FACTS_VALIDATE = "reference_facts:validate"  # validate facts and value bands
    PROFILES_READ = "profiles:read"
    PROFILES_MANAGE = "profiles:manage"  # activate, roll back (admin only)
    LEVELS_OVERRIDE = "levels:override"


class Role(StrEnum):
    REVIEWER = "reviewer"  # takes a dossier into review
    EDITOR = "editor"  # validates, rejects and marks as published
    ADMIN = "admin"  # everything, including team management


class ActorKind(StrEnum):
    SYSTEM = "system"  # internal processes (hangar, radar)
    SERVICE = "service"  # client authenticated by service token (bot…)
    STAFF = "staff"  # team member, acting via a service (Discord ID)
    INSTALLATION = "installation"  # Chromium extension (anonymous token)
    ANONYMOUS = "anonymous"  # public website


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.REVIEWER: frozenset(
        {
            Permission.DOSSIER_READ_PUBLIC,
            Permission.DOSSIER_READ,
            Permission.DOSSIER_CREATE,
            Permission.DOSSIER_REVIEW,
            Permission.WATCHLIST_READ,
            Permission.FACTS_WRITE,
            Permission.PROFILES_READ,
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
            Permission.WATCHLIST_READ,
            Permission.WATCHLIST_WRITE,
            Permission.USAGE_READ,
            Permission.FACTCHECK_REQUEST,
            Permission.GROUPS_MANAGE,
            Permission.FACTS_WRITE,
            Permission.FACTS_VALIDATE,
            Permission.PROFILES_READ,
            Permission.LEVELS_OVERRIDE,
        }
    ),
    Role.ADMIN: frozenset(Permission),
}

PUBLIC_PERMISSIONS = frozenset({Permission.DOSSIER_READ_PUBLIC})
INSTALLATION_PERMISSIONS = frozenset({Permission.CONTENT_INGEST})


@dataclass(frozen=True, slots=True)
class Actor:
    kind: ActorKind
    id: str | None = None
    permissions: frozenset[Permission] = field(default_factory=frozenset)
    via: str | None = None
    """Service through which a team member acts (e.g. ``intercom``)."""

    @property
    def label(self) -> str:
        """Stable representation for audit logs (``staff:1234@intercom``)."""
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
                f"Missing permission: {permission.value}", permission=permission.value
            )

    # --- Factories --------------------------------------------------------------
    @classmethod
    def system(cls, name: str) -> Actor:
        return cls(ActorKind.SYSTEM, name, frozenset(Permission))

    @classmethod
    def anonymous(cls) -> Actor:
        return cls(ActorKind.ANONYMOUS, None, PUBLIC_PERMISSIONS)

    @classmethod
    def installation(cls) -> Actor:
        """Chromium extension. Anonymous: the token is never part of the actor."""
        return cls(ActorKind.INSTALLATION, None, INSTALLATION_PERMISSIONS)

    @classmethod
    def service(cls, name: str, scopes: frozenset[Permission]) -> Actor:
        return cls(ActorKind.SERVICE, name, scopes | PUBLIC_PERMISSIONS)

    @classmethod
    def staff(cls, discord_id: int, roles: frozenset[Role], via: Actor) -> Actor:
        """Member acting via a service: rights = roles ∩ the service's scopes."""
        granted: set[Permission] = set()
        for role in roles:
            granted |= ROLE_PERMISSIONS[role]
        return cls(
            ActorKind.STAFF,
            str(discord_id),
            frozenset(granted) & via.permissions | PUBLIC_PERMISSIONS,
            via=via.id,
        )
