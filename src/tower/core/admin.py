"""Opérations d'administration lancées depuis la CLI (``railway run tower …``)."""

from __future__ import annotations

import sys

from sqlalchemy.dialects.postgresql import insert

from tower.core.auth.tokens import generate_token, hash_token
from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain.errors import InvalidInput
from tower.core.domain.security import Actor, Permission, Role
from tower.core.errors import service as errors_service
from tower.core.models.auth import ServiceClient, StaffMember


async def create_service_client(settings: Settings, name: str, scopes: list[str]) -> str:
    unknown = [s for s in scopes if s not in Permission]
    if unknown:
        raise InvalidInput(
            f"Scopes inconnus : {unknown} (connus : {[p.value for p in Permission]})"
        )
    token = generate_token()
    db = Database(settings)
    try:
        async with db.transaction() as session:
            session.add(ServiceClient(name=name, token_hash=hash_token(token), scopes=scopes))
    finally:
        await db.dispose()
    return token


async def upsert_staff(
    settings: Settings, discord_id: int, roles: list[str], name: str | None, active: bool
) -> None:
    unknown = [r for r in roles if r not in Role]
    if unknown:
        raise InvalidInput(f"Rôles inconnus : {unknown}")
    db = Database(settings)
    try:
        async with db.transaction() as session:
            stmt = insert(StaffMember).values(
                discord_id=discord_id, roles=roles, display_name=name, active=active
            )
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[StaffMember.discord_id],
                    set_={
                        "roles": stmt.excluded.roles,
                        "display_name": stmt.excluded.display_name,
                        "active": stmt.excluded.active,
                        "updated_at": stmt.excluded.updated_at,
                    },
                )
            )
    finally:
        await db.dispose()


async def list_errors(settings: Settings, *, include_resolved: bool) -> None:
    db = Database(settings)
    try:
        async with db.session() as session:
            rows = await errors_service.list_reports(
                session, Actor.system("cli"), include_resolved=include_resolved, limit=200
            )
    finally:
        await db.dispose()
    for r in rows:
        state = "résolue" if r.resolved_at else "OUVERTE"
        sys.stdout.write(
            f"{r.fingerprint}  {r.code:<13} {r.severity:<8} x{r.occurrences:<6} "
            f"{r.last_seen:%Y-%m-%d %H:%M}  {state:<8} {r.title} — {r.location or '-'}\n"
        )


async def resolve_error(settings: Settings, fingerprint: str, note: str | None) -> None:
    db = Database(settings)
    try:
        async with db.transaction() as session:
            await errors_service.resolve(session, Actor.system("cli"), fingerprint, note=note)
    finally:
        await db.dispose()
