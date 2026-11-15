"""Actors, roles and permissions.

An actor is resolved on each request (gate) or set by the process (system).
Permissions are checked in the use cases, not only in the API: a new client
(bot, script) therefore cannot bypass them.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from tower.core.domain import error_codes as ec
from tower.core.domain.errors import InvalidInput, PermissionDenied


class Permission(StrEnum):
    DOSSIER_READ_PUBLIC = "dossiers:read_public"
    DOSSIER_READ = "dossiers:read"
    DOSSIER_CREATE = "dossiers:create"
    DOSSIER_REVIEW = "dossiers:review"
    DOSSIER_DECIDE = "dossiers:decide"
    DOSSIER_PUBLISH = "dossiers:publish"
    EVENTS_READ = "events:read"
    STAFF_READ = "staff:read"  # list the ranked team and read a member
    STAFF_MANAGE = "staff:manage"  # rank and unrank members
    PERMISSIONS_MANAGE_OTHERS = "permissions:manage_others"
    PERMISSIONS_MANAGE_SELF = "permissions:manage_self"
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
    REGISTRY_READ = "registry:read"  # reliability and country registries (lot 6)
    REGISTRY_WRITE = "registry:write"  # edit them (editors)


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
            Permission.REGISTRY_READ,
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
            Permission.REGISTRY_READ,
            Permission.REGISTRY_WRITE,
        }
    ),
    Role.ADMIN: frozenset(Permission),
}

PUBLIC_PERMISSIONS = frozenset({Permission.DOSSIER_READ_PUBLIC})
INSTALLATION_PERMISSIONS = frozenset({Permission.CONTENT_INGEST})

NOT_GRANTABLE = frozenset(
    {Permission.DOSSIER_READ_PUBLIC, Permission.CONTENT_INGEST, Permission.EVENTS_READ}
)
"""Permissions that belong to the service or the public, never to a member."""
GRANTABLE_PERMISSIONS: frozenset[Permission] = frozenset(Permission) - NOT_GRANTABLE
"""Permissions that can be granted individually to a ranked team member."""


def parse_grantable(values: Iterable[str]) -> frozenset[Permission]:
    """Validate permission names for an individual grant (``InvalidInput`` otherwise)."""
    out: set[Permission] = set()
    for value in values:
        try:
            permission = Permission(value)
        except ValueError:
            raise InvalidInput(
                "Unknown permission", code=ec.STF_BAD_PERMISSION, permission=str(value)[:64]
            ) from None
        if permission not in GRANTABLE_PERMISSIONS:
            raise InvalidInput(
                "Permission cannot be granted to a member",
                code=ec.STF_BAD_PERMISSION,
                permission=permission.value,
            )
        out.add(permission)
    return frozenset(out)


def role_permissions(roles: Iterable[str]) -> frozenset[Permission]:
    """Union of the permissions of the (known) roles, restricted to grantable ones."""
    granted: set[Permission] = set()
    for role in roles:
        if role in Role:
            granted |= ROLE_PERMISSIONS[Role(role)]
    return frozenset(granted) & GRANTABLE_PERMISSIONS


def member_permissions(
    roles: Iterable[str], permissions: Iterable[str], *, is_admin: bool
) -> frozenset[Permission]:
    """What a member holds: everything for a hard-coded admin, else the union of the
    permissions of the roles (presets) and the individual ones."""
    if is_admin:
        return GRANTABLE_PERMISSIONS
    individual = {Permission(p) for p in permissions if p in Permission}
    return role_permissions(roles) | (frozenset(individual) & GRANTABLE_PERMISSIONS)


def permissions_change_rules(
    *,
    actor_holds: frozenset[Permission],
    actor_is_admin: bool,
    current: frozenset[Permission],
    new: frozenset[Permission],
) -> tuple[frozenset[Permission], frozenset[Permission]]:
    """Return ``(added, removed)``; a non-admin actor may only add or remove permissions
    he holds himself (anti-escalation, ``TWR-STF-006``)."""
    added, removed = new - current, current - new
    if not actor_is_admin:
        forbidden = (added | removed) - actor_holds
        if forbidden:
            raise PermissionDenied(
                "You can only grant or remove permissions you hold yourself",
                code=ec.STF_ESCALATION,
                permissions=sorted(p.value for p in forbidden),
            )
    return added, removed


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
    def staff(
        cls,
        discord_id: int,
        roles: frozenset[Role],
        via: Actor,
        permissions: frozenset[Permission] = frozenset(),
        *,
        is_admin: bool = False,
    ) -> Actor:
        """Member acting via a service: rights = (role presets ∪ individual permissions,
        or everything for a hard-coded admin) ∩ the service's scopes."""
        granted: set[Permission] = set(permissions)
        for role in roles:
            granted |= ROLE_PERMISSIONS[role]
        if is_admin:
            granted = set(Permission)
        return cls(
            ActorKind.STAFF,
            str(discord_id),
            frozenset(granted) & via.permissions | PUBLIC_PERMISSIONS,
            via=via.id,
        )
