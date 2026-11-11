"""Admin operations run from the CLI (``railway run tower …``)."""

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
        raise InvalidInput(f"Unknown scopes: {unknown} (known: {[p.value for p in Permission]})")
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
        raise InvalidInput(f"Unknown roles: {unknown}")
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
        state = "resolved" if r.resolved_at else "OPEN"
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


async def publish_filters_from_file(settings: Settings, path: str, version: str) -> None:
    """Publish and activate extension filters read from a JSON file."""
    import asyncio
    import json
    from pathlib import Path

    from tower.core.domain import error_codes as ec
    from tower.core.usecases.ingestion import publish_filters

    try:
        filters = json.loads(await asyncio.to_thread(Path(path).read_text, encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InvalidInput(
            f"Cannot read filters file: {exc}", code=ec.ING_BAD_FILTERS, path=path
        ) from exc
    db = Database(settings)
    try:
        async with db.transaction() as session:
            row = await publish_filters(
                session, Actor.system("cli"), version=version, filters=filters
            )
            summary = (
                f"{len(row.filters['keywords'])} keywords, {len(row.filters['accounts'])} accounts"
            )
    finally:
        await db.dispose()
    sys.stdout.write(f"Published extension filters version {version} ({summary})\n")
