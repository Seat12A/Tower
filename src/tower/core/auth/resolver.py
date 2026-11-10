"""Résolution d'un acteur à partir des informations d'une requête.

Petit cache mémoire à durée courte (``TOWER_AUTH_CACHE_TTL``) pour éviter une requête
SQL par appel ; une révocation prend effet au plus tard après ce délai.
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select

from tower.core.auth.tokens import hash_token
from tower.core.db.database import Database
from tower.core.domain import error_codes as ec
from tower.core.domain.errors import PermissionDenied, Unauthenticated
from tower.core.domain.security import Actor, Permission, Role
from tower.core.models.auth import ServiceClient, StaffMember

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

    def put(self, key: Any, value: Any) -> None:
        if len(self._data) >= _MAX_ENTRIES:
            self._data.clear()
        self._data[key] = (time.monotonic() + self.ttl, value)


class ActorResolver:
    def __init__(self, db: Database, *, cache_ttl: float = 15.0) -> None:
        self.db = db
        self._services = _TTLCache(cache_ttl)
        self._staff = _TTLCache(cache_ttl)

    async def resolve(self, token: str | None, acting_discord_id: str | None) -> Actor:
        if not token:
            if acting_discord_id:
                raise Unauthenticated(
                    "X-Acting-Discord-Id exige un token de service",
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
                "X-Acting-Discord-Id invalide", code=ec.AUTH_BAD_ACTING_ID
            ) from None
        roles = await self._staff_roles(discord_id)
        if roles is None:
            raise PermissionDenied(
                "Membre inconnu ou inactif",
                code=ec.AUTH_UNKNOWN_STAFF,
                discord_id=str(discord_id),
            )
        return Actor.staff(discord_id, roles, via=service)

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
            raise Unauthenticated(
                "Token de service invalide ou révoqué", code=ec.AUTH_INVALID_TOKEN
            )
        scopes = frozenset(Permission(s) for s in client.scopes if s in Permission)
        actor = Actor.service(client.name, scopes)
        self._services.put(digest, actor)
        return actor

    async def _staff_roles(self, discord_id: int) -> frozenset[Role] | None:
        cached = self._staff.get(discord_id)
        if cached is not None:
            return cached or None
        async with self.db.session() as session:
            member = await session.get(StaffMember, discord_id)
        roles: frozenset[Role] = (
            frozenset(Role(r) for r in member.roles if r in Role)
            if member is not None and member.active
            else frozenset()
        )
        self._staff.put(discord_id, roles)
        return roles or None
