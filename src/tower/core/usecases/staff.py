"""Team ranks and individual permissions.

A member must be *ranked* (an active ``staff_members`` row) to receive permissions.
Hard-coded administrators (``TOWER_ADMIN_DISCORD_IDS``) hold every permission, need no
row, and can be neither ranked, unranked nor edited. Roles stay as presets for the CLI:
the first permission edit materialises them into the explicit ``permissions`` list.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain import error_codes as ec
from tower.core.domain.errors import Conflict, InvalidInput, NotFound, PermissionDenied
from tower.core.domain.security import (
    GRANTABLE_PERMISSIONS,
    Actor,
    ActorKind,
    Permission,
    member_permissions,
    parse_grantable,
    permissions_change_rules,
)
from tower.core.events.outbox import publish
from tower.core.logging import get_logger
from tower.core.models.auth import StaffMember

log = get_logger(__name__)

AGGREGATE = "staff_member"
_MAX_NAME = 100


@dataclass(frozen=True, slots=True)
class Member:
    """A ranked member as served by the API (admins without a row are synthesised)."""

    discord_id: int
    display_name: str | None
    active: bool
    is_admin: bool
    roles: tuple[str, ...]
    permissions: tuple[str, ...]
    """Explicit individual permissions."""
    effective_permissions: tuple[str, ...]
    """Everything the member holds (roles + individual; all for an admin)."""
    created_at: datetime | None
    updated_at: datetime | None


def _member(row: StaffMember | None, discord_id: int, admin_ids: Collection[int]) -> Member:
    is_admin = discord_id in admin_ids
    if row is None:
        return Member(
            discord_id, None, True, is_admin, (), (), _values(GRANTABLE_PERMISSIONS), None, None
        )
    effective = member_permissions(row.roles, row.permissions, is_admin=is_admin)
    return Member(
        row.discord_id,
        row.display_name,
        row.active,
        is_admin,
        tuple(row.roles),
        tuple(sorted(row.permissions)),
        _values(effective),
        row.created_at,
        row.updated_at,
    )


def _values(permissions: Iterable[Permission]) -> tuple[str, ...]:
    return tuple(sorted(p.value for p in permissions))


def _name(value: str | None) -> str | None:
    if value is None:
        return None
    name = value.strip()
    if len(name) > _MAX_NAME:
        raise InvalidInput("Display name too long", max_chars=_MAX_NAME)
    return name or None


def _guard_target(actor: Actor, discord_id: int, admin_ids: Collection[int]) -> None:
    """Rank/unrank: never yourself, never a hard-coded administrator."""
    if actor.discord_id == discord_id:
        raise PermissionDenied("You cannot rank or unrank yourself", code=ec.STF_SELF_FORBIDDEN)
    if discord_id in admin_ids:
        raise Conflict(
            "Hard-coded administrators cannot be ranked or unranked",
            code=ec.STF_ADMIN_PROTECTED,
            discord_id=str(discord_id),
        )


async def _locked(session: AsyncSession, discord_id: int) -> StaffMember | None:
    return await session.scalar(
        select(StaffMember).where(StaffMember.discord_id == discord_id).with_for_update()
    )


def _not_ranked(discord_id: int) -> NotFound:
    return NotFound("Member is not ranked", code=ec.STF_NOT_RANKED, discord_id=str(discord_id))


async def rank(
    session: AsyncSession,
    actor: Actor,
    discord_id: int,
    display_name: str | None,
    admin_ids: Collection[int],
) -> Member:
    """Create or reactivate a team member (no permission beyond the roles/permissions
    already stored)."""
    actor.require(Permission.STAFF_MANAGE)
    _guard_target(actor, discord_id, admin_ids)
    name = _name(display_name)
    row = await _locked(session, discord_id)
    reactivated = row is not None
    if row is None:
        row = (
            await session.execute(
                insert(StaffMember)
                .values(
                    discord_id=discord_id, display_name=name, roles=[], permissions=[], active=True
                )
                .returning(StaffMember)
            )
        ).scalar_one()
    elif row.active:
        raise Conflict(
            "Member is already ranked", code=ec.STF_ALREADY_RANKED, discord_id=str(discord_id)
        )
    else:
        row.active = True
        row.updated_at = func.now()
        if name is not None:
            row.display_name = name
        await session.flush()
    await session.refresh(row)
    await publish(
        session,
        "staff.ranked",
        {
            "discord_id": str(discord_id),
            "display_name": row.display_name,
            "reactivated": reactivated,
        },
        aggregate_type=AGGREGATE,
        aggregate_id=str(discord_id),
        actor=actor.label,
    )
    log.info("staff.ranked", discord_id=str(discord_id), by=actor.label)
    return _member(row, discord_id, admin_ids)


async def unrank(
    session: AsyncSession, actor: Actor, discord_id: int, admin_ids: Collection[int]
) -> None:
    """Deactivate a member and wipe their roles and permissions."""
    actor.require(Permission.STAFF_MANAGE)
    _guard_target(actor, discord_id, admin_ids)
    row = await _locked(session, discord_id)
    if row is None or not row.active:
        raise _not_ranked(discord_id)
    removed = list(member_permissions(row.roles, row.permissions, is_admin=False))
    row.active = False
    row.roles = []
    row.permissions = []
    row.updated_at = func.now()
    await session.flush()
    await publish(
        session,
        "staff.unranked",
        {"discord_id": str(discord_id), "removed": _values(removed)},
        aggregate_type=AGGREGATE,
        aggregate_id=str(discord_id),
        actor=actor.label,
    )
    log.info("staff.unranked", discord_id=str(discord_id), by=actor.label)


async def set_permissions(
    session: AsyncSession,
    actor: Actor,
    discord_id: int,
    permissions: Iterable[str],
    admin_ids: Collection[int],
) -> Member:
    """Replace the individual permissions of a ranked member by ``permissions``."""
    is_self = actor.discord_id == discord_id
    actor.require(
        Permission.PERMISSIONS_MANAGE_SELF if is_self else Permission.PERMISSIONS_MANAGE_OTHERS
    )
    if discord_id in admin_ids:
        raise Conflict(
            "Hard-coded administrators hold every permission and cannot be edited",
            code=ec.STF_ADMIN_PROTECTED,
            discord_id=str(discord_id),
        )
    new = parse_grantable(permissions)
    row = await _locked(session, discord_id)
    if row is None or not row.active:
        raise PermissionDenied(
            "Only ranked members can receive permissions",
            code=ec.STF_TARGET_NOT_RANKED,
            discord_id=str(discord_id),
        )
    current = member_permissions(row.roles, row.permissions, is_admin=False)
    actor_is_admin = actor.discord_id is not None and actor.discord_id in admin_ids
    added, removed = permissions_change_rules(
        actor_holds=actor.permissions & GRANTABLE_PERMISSIONS,
        actor_is_admin=actor_is_admin,
        current=current,
        new=new,
    )
    # The explicit list becomes the single source of truth: roles are cleared.
    row.permissions = sorted(p.value for p in new)
    row.roles = []
    row.updated_at = func.now()
    await session.flush()
    await session.refresh(row)
    if added or removed:
        await publish(
            session,
            "staff.permissions_changed",
            {
                "discord_id": str(discord_id),
                "added": list(_values(added)),
                "removed": list(_values(removed)),
            },
            aggregate_type=AGGREGATE,
            aggregate_id=str(discord_id),
            actor=actor.label,
        )
        log.info(
            "staff.permissions_changed",
            discord_id=str(discord_id),
            by=actor.label,
            added=len(added),
            removed=len(removed),
        )
    return _member(row, discord_id, admin_ids)


async def get_member(
    session: AsyncSession, actor: Actor, discord_id: int, admin_ids: Collection[int]
) -> Member:
    """``staff:read``, or the member themself."""
    if actor.discord_id != discord_id:
        actor.require(Permission.STAFF_READ)
    row = await session.get(StaffMember, discord_id)
    if row is None and discord_id not in admin_ids:
        raise _not_ranked(discord_id)
    return _member(row, discord_id, admin_ids)


async def list_members(
    session: AsyncSession,
    actor: Actor,
    admin_ids: Collection[int],
    *,
    include_inactive: bool = False,
) -> list[Member]:
    """Ranked members (hard-coded administrators included, listed first)."""
    actor.require(Permission.STAFF_READ)
    stmt = select(StaffMember).order_by(StaffMember.created_at, StaffMember.discord_id)
    if not include_inactive:
        stmt = stmt.where(StaffMember.active.is_(True))
    rows = {r.discord_id: r for r in (await session.scalars(stmt)).all()}
    out = [_member(rows.pop(i, None), i, admin_ids) for i in sorted(admin_ids)]
    out += [_member(r, r.discord_id, admin_ids) for r in rows.values()]
    return out


async def me(session: AsyncSession, actor: Actor, admin_ids: Collection[int]) -> Member:
    """The acting member (any staff actor); a hard-coded admin is synthesised."""
    discord_id = actor.discord_id
    if actor.kind is not ActorKind.STAFF or discord_id is None:
        raise PermissionDenied("A team member is required (X-Acting-Discord-Id)")
    row = await session.get(StaffMember, discord_id)
    return _member(row, discord_id, admin_ids)
