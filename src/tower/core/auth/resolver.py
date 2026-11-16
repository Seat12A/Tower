"""Resolve an actor from the information in a request.

Small short-lived in-memory cache (``TOWER_AUTH_CACHE_TTL``) to avoid one SQL query
per call; a revocation takes effect at the latest after this delay.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select

from tower.core.auth.tokens import hash_token
from tower.core.db.database import Database
from tower.core.domain import error_codes as ec
from tower.core.domain.errors import PermissionDenied, Unauthenticated
from tower.core.domain.security import Actor, Permission, Role, to_permission
from tower.core.models.auth import ServiceClient, StaffMember
from tower.core.models.badges import Badge, BadgeOrgKey

_MAX_ENTRIES = 1024


class _TTLCache:
    def __init__(self, ttl: float) -> None:
        self.ttl = ttl
        self._data: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any) -> Any:
        hit = self._data.get(key)
        if hit is None or hit[0] < time.monotonic():
            return None
        return hit[1]

    def drop(self, key: Any) -> None:
        self._data.pop(key, None)

    def put(self, key: Any, value: Any) -> None:
        if len(self._data) >= _MAX_ENTRIES:
            self._data.clear()
        self._data[key] = (time.monotonic() + self.ttl, value)


class ActorResolver:
    def __init__(
        self, db: Database, *, cache_ttl: float = 15.0, admin_ids: Iterable[int] = ()
    ) -> None:
        self.db = db
        self.admin_ids = frozenset(admin_ids)
        self._services = _TTLCache(cache_ttl)
        self._staff_cache = _TTLCache(cache_ttl)
        self._orgs = _TTLCache(cache_ttl)

    async def resolve(self, token: str | None, acting_discord_id: str | None) -> Actor:
        if not token:
            if acting_discord_id:
                raise Unauthenticated(
                    "X-Acting-Discord-Id requires a service token",
                    code=ec.AUTH_ACTING_WITHOUT_TOKEN,
                )
            return Actor.anonymous()
        service = await self._service(token)
        if acting_discord_id is None:
            return service
        try:
            discord_id = int(acting_discord_id)
        except ValueError:
            raise Unauthenticated(
                "Invalid X-Acting-Discord-Id", code=ec.AUTH_BAD_ACTING_ID
            ) from None
        is_admin = discord_id in self.admin_ids
        member = await self._staff(discord_id)
        if member is None and not is_admin:
            raise PermissionDenied(
                "Unknown or inactive member",
                code=ec.AUTH_UNKNOWN_STAFF,
                discord_id=str(discord_id),
            )
        roles, permissions = member or (frozenset(), frozenset())
        return Actor.staff(discord_id, roles, service, permissions, is_admin=is_admin)

    async def resolve_organization(self, token: str) -> Actor:
        """Organisation key (``X-Organization-Token``): valid while the key is not revoked
        and the organisation badge is active. Never combined with another credential."""
        digest = hash_token(token)
        cached = self._orgs.get(digest)
        if cached is not None:
            return cached  # type: ignore[no-any-return]
        async with self.db.session() as session:
            public_id = await session.scalar(
                select(Badge.public_id)
                .join(BadgeOrgKey, BadgeOrgKey.org_badge_id == Badge.id)
                .where(
                    BadgeOrgKey.token_hash == digest,
                    BadgeOrgKey.revoked_at.is_(None),
                    Badge.status == "active",
                )
            )
        if public_id is None:
            raise Unauthenticated(
                "Organisation token is invalid or revoked", code=ec.BDG_ORG_TOKEN_INVALID
            )
        actor = Actor.organization(str(public_id))
        self._orgs.put(digest, actor)
        return actor

    def forget(self, discord_id: int) -> None:
        """Drop the cached entry of a member (after a rank or permission change)."""
        self._staff_cache.drop(discord_id)

    async def _service(self, token: str) -> Actor:
        digest = hash_token(token)
        cached = self._services.get(digest)
        if cached is not None:
            return cached  # type: ignore[no-any-return]
        async with self.db.session() as session:
            client = await session.scalar(
                select(ServiceClient).where(
                    ServiceClient.token_hash == digest, ServiceClient.revoked_at.is_(None)
                )
            )
        if client is None:
            raise Unauthenticated("Service token is invalid or revoked", code=ec.AUTH_INVALID_TOKEN)
        scopes = frozenset(q for s in client.scopes if (q := to_permission(s)) is not None)
        actor = Actor.service(client.name, scopes)
        self._services.put(digest, actor)
        return actor

    async def _staff(self, discord_id: int) -> tuple[frozenset[Role], frozenset[Permission]] | None:
        """Roles and individual permissions of an active ranked member, else ``None``."""
        cached = self._staff_cache.get(discord_id)
        if cached is not None:
            return cached or None
        async with self.db.session() as session:
            member = await session.get(StaffMember, discord_id)
        found: tuple[frozenset[Role], frozenset[Permission]] | tuple[()] = ()
        if member is not None and member.active:
            found = (
                frozenset(Role(r) for r in member.roles if r in Role),
                frozenset(q for p in member.permissions if (q := to_permission(p)) is not None),
            )
        self._staff_cache.put(discord_id, found)
        return found or None
