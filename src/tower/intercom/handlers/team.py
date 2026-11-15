"""Team area: ``/team``, ``/rank``, ``/unrank``, ``/permissions`` and ``/help`` cards.

Pure logic (no discord import). Permissions and ranks are decided by **gate**; the bot adds
only the Discord role hierarchy (``can_act_on``), computed by the command layer from
``member.top_role.position`` and passed in as plain integers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tower.intercom.gate_client import GateClient, GateError
from tower.intercom.handlers import (
    Button,
    Field,
    Option,
    Reply,
    Select,
    card,
    code,
    error_card,
    guarded,
    listing,
    success,
)
from tower.intercom.i18n import t
from tower.intercom.ui import emojis

__all__ = [
    "ALL_PERMISSIONS",
    "CATEGORIES",
    "CID_TEAM_LIST",
    "CID_TEAM_OPEN",
    "COMMAND_AREAS",
    "COMMAND_PERMISSIONS",
    "Category",
    "can_act_on",
    "cid_perm_clear",
    "cid_perm_set",
    "cid_perms",
    "cid_unrank",
    "clear_permissions",
    "command_allowed",
    "editable_permissions",
    "help_card",
    "member_header",
    "new_permissions",
    "permissions_panel",
    "rank",
    "set_category",
    "team_list",
    "unrank_cancelled",
    "unrank_confirm",
    "unrank_do",
]

CID_TEAM_OPEN = "t12a:team:open"
CID_TEAM_LIST = "t12a:team:list"


def cid_perms(target: int | str) -> str:
    """Button that opens the permissions panel of ``target``."""
    return f"t12a:team:perms:{target}"


def cid_unrank(action: str, target: int | str) -> str:
    """``action``: ``yes`` or ``no`` (the confirmation of ``/unrank``)."""
    return f"t12a:team:unrank_{action}:{target}"


def cid_perm_set(target: int | str, category: str) -> str:
    return f"t12a:perm:set:{target}:{category}"


def cid_perm_clear(action: str, target: int | str) -> str:
    """``action``: ``ask`` (button), ``yes`` or ``no`` (in-place confirmation)."""
    return f"t12a:perm:clear_{action}:{target}"


@dataclass(frozen=True, slots=True)
class Category:
    id: str
    icon: str
    permissions: tuple[str, ...]


CATEGORIES: tuple[Category, ...] = (
    Category(
        "dossiers",
        emojis.FOLDER,
        (
            "dossiers:read",
            "dossiers:create",
            "dossiers:review",
            "dossiers:decide",
            "dossiers:publish",
        ),
    ),
    Category(
        "factcheck",
        "",
        (
            "factcheck:request",
            "claim_groups:manage",
            "reference_facts:write",
            "reference_facts:validate",
            "levels:override",
        ),
    ),
    Category("watchlist", emojis.EYE, ("watchlist:read", "watchlist:write")),
    Category("registry", emojis.BOOK, ("registry:read", "registry:write")),
    Category("profiles", emojis.SETTINGS, ("profiles:read", "profiles:manage")),
    Category("supervision", emojis.MONITOR_HEART, ("usage:read", "errors:read", "errors:manage")),
    Category(
        "team",
        emojis.GROUPS,
        (
            "staff:read",
            "staff:manage",
            "permissions:manage_others",
            "permissions:manage_self",
        ),
    ),
)
"""Every grantable permission, grouped for the permissions panel (one select each)."""

ALL_PERMISSIONS: frozenset[str] = frozenset(p for c in CATEGORIES for p in c.permissions)

PERM_MANAGE_OTHERS = "permissions:manage_others"
PERM_MANAGE_SELF = "permissions:manage_self"

# --- Command -> required permission (``None``: any team member) ---------------------
_COMMANDS: tuple[tuple[str, str, str | None], ...] = (
    ("dossiers", "dossier show", "dossiers:read"),
    ("dossiers", "dossier take", "dossiers:review"),
    ("dossiers", "dossier validate", "dossiers:decide"),
    ("dossiers", "dossier reject", "dossiers:decide"),
    ("dossiers", "dossier publish", "dossiers:publish"),
    ("factcheck", "factcheck", "factcheck:request"),
    ("factcheck", "group show", "dossiers:read"),
    ("factcheck", "group merge", "claim_groups:manage"),
    ("factcheck", "group split", "claim_groups:manage"),
    ("factcheck", "fact show", None),
    ("factcheck", "fact list", None),
    ("factcheck", "fact validate", "reference_facts:validate"),
    ("factcheck", "fact reject", "reference_facts:validate"),
    ("factcheck", "fact band", "reference_facts:validate"),
    ("factcheck", "narrative list", "dossiers:read"),
    ("factcheck", "narrative show", "dossiers:read"),
    ("factcheck", "narrative merge", "claim_groups:manage"),
    ("factcheck", "narrative split", "claim_groups:manage"),
    ("factcheck", "narrative title", "dossiers:review"),
    ("factcheck", "level set", "levels:override"),
    ("factcheck", "level list", "dossiers:read"),
    ("factcheck", "level revoke", "levels:override"),
    ("watchlist", "watchlist list", "watchlist:read"),
    ("watchlist", "watchlist add", "watchlist:write"),
    ("watchlist", "watchlist edit", "watchlist:write"),
    ("watchlist", "watchlist deactivate", "watchlist:write"),
    ("registry", "registry show", "registry:read"),
    ("registry", "registry history", "registry:read"),
    ("registry", "registry set-tier", "registry:write"),
    ("registry", "registry set-control", "registry:write"),
    ("supervision", "incidents list", "errors:read"),
    ("supervision", "incidents show", "errors:read"),
    ("supervision", "incidents resolve", "errors:manage"),
    ("supervision", "usage", "usage:read"),
    ("supervision", "profile show", "profiles:read"),
    ("supervision", "profile history", "profiles:read"),
    ("supervision", "profile set", "profiles:manage"),
    ("supervision", "profile rollback", "profiles:manage"),
    ("team", "team", "staff:read"),
    ("team", "rank", "staff:manage"),
    ("team", "unrank", "staff:manage"),
    ("team", "permissions", None),
    ("team", "help", None),
)
COMMAND_PERMISSIONS: dict[str, str | None] = {c: p for _, c, p in _COMMANDS}
"""Static map used by ``/help``; a command missing from it is shown to everyone."""
COMMAND_AREAS: tuple[tuple[str, tuple[str, ...]], ...] = tuple(
    (area, tuple(c for a, c, _ in _COMMANDS if a == area))
    for area in dict.fromkeys(a for a, _, _ in _COMMANDS)
)


def command_allowed(command: str, permissions: Collection[str]) -> bool:
    """Whether ``/help`` lists ``command`` for a member holding ``permissions``."""
    required = COMMAND_PERMISSIONS.get(command)
    return required is None or required in permissions


# --- Pure rules -------------------------------------------------------------------
def can_act_on(
    actor_top_position: int | None,
    target_top_position: int | None,
    actor_is_admin: bool,
    target_is_admin: bool,
    is_self: bool,
) -> bool:
    """Discord role hierarchy: a member acts only on someone whose highest role is strictly
    below theirs. Administrators bypass it; nobody but an administrator acts on one; acting
    on yourself is always allowed (gate decides). A target outside the guild has no role
    (position -1); an unknown actor position is refused."""
    if is_self or actor_is_admin:
        return True
    if target_is_admin or actor_top_position is None:
        return False
    return actor_top_position > (-1 if target_top_position is None else target_top_position)


def editable_permissions(
    actor_permissions: Collection[str], actor_is_admin: bool, category: Category
) -> tuple[str, ...]:
    """Permissions of ``category`` the actor may grant or remove: all for an administrator,
    else only those he holds himself (anti-escalation, also enforced by gate)."""
    if actor_is_admin:
        return category.permissions
    return tuple(p for p in category.permissions if p in actor_permissions)


def new_permissions(
    current: Collection[str], options: Collection[str], selected: Collection[str]
) -> frozenset[str]:
    """``(current - options) | (selected & options)``: a submitted select only decides
    the permissions it offered; a forged value outside ``options`` is ignored."""
    return (frozenset(current) - frozenset(options)) | (frozenset(selected) & frozenset(options))


def _category(category_id: str) -> Category | None:
    return next((c for c in CATEGORIES if c.id == category_id), None)


def _name(locale: str, member: Mapping[str, Any]) -> str:
    return str(member.get("display_name") or t(locale, "team.unnamed"))


def _mention(member: Mapping[str, Any]) -> str:
    return f"<@{member['discord_id']}>"


def member_header(locale: str, member: Mapping[str, Any]) -> str:
    """``<owner icon> @mention · **name**`` (the member card header)."""
    owner = f"{emojis.OWNER} " if member.get("is_admin") else ""
    return f"{owner}{_mention(member)} · **{_name(locale, member)}**"


def _hierarchy_card(locale: str, *, target_is_admin: bool) -> Reply:
    key = "hierarchy.admin.body" if target_is_admin else "hierarchy.body"
    return error_card(t(locale, "hierarchy.title"), t(locale, key), severity="warning")


# --- Team list --------------------------------------------------------------------
@guarded
async def team_list(gate: GateClient, acting: int, locale: str) -> Reply:
    members = await gate.list_staff(acting)
    if not members:
        return card(t(locale, "team.title"), t(locale, "team.empty"), icon=emojis.GROUPS)
    lines = [
        f"{member_header(locale, m)} · "
        f"{t(locale, 'team.item.perms', count=len(m.get('effective_permissions', ())))}"
        for m in members
    ]
    items, more = listing(locale, lines)
    shown = members[: len(items)]
    options = tuple(
        Option(
            value=str(m["discord_id"]),
            label=_name(locale, m),
            description=t(locale, "team.admin")
            if m.get("is_admin")
            else t(locale, "team.option.perms", count=len(m.get("effective_permissions", ()))),
            icon=emojis.OWNER if m.get("is_admin") else "",
        )
        for m in shown
    )
    return card(
        t(locale, "team.title"),
        icon=emojis.GROUPS,
        items=items,
        more=more,
        fields=(
            Field(
                "",
                Select(
                    CID_TEAM_OPEN,
                    placeholder=t(locale, "team.select.placeholder"),
                    options=options,
                ),
            ),
        ),
    )


# --- Rank / unrank ----------------------------------------------------------------
async def _lookup(gate: GateClient, acting: int, target_id: int) -> Mapping[str, Any] | None:
    """The target as gate knows it, or ``None`` (not ranked, or acting member may not read)."""
    try:
        return await gate.get_staff(acting, target_id)
    except GateError as exc:
        if exc.status in (403, 404):
            return None
        raise


@guarded
async def rank(
    gate: GateClient,
    acting: int,
    locale: str,
    target_id: int,
    display_name: str | None,
    actor_top: int | None,
    target_top: int | None,
) -> Reply:
    me = await gate.staff_me(acting)
    target = await _lookup(gate, acting, target_id)
    target_is_admin = bool(target and target.get("is_admin"))
    if not can_act_on(
        actor_top, target_top, bool(me.get("is_admin")), target_is_admin, acting == target_id
    ):
        return _hierarchy_card(locale, target_is_admin=target_is_admin)
    member = await gate.rank_staff(acting, target_id, display_name=display_name)
    return success(
        t(locale, "rank.title"),
        t(locale, "rank.body", mention=_mention(member)),
        details=((t(locale, "team.title"), code(_name(locale, member))),),
        buttons=(
            Button(
                t(locale, "rank.button"),
                cid_perms(member["discord_id"]),
                icon=emojis.PERMISSIONS,
            ),
        ),
    )


@guarded
async def unrank_confirm(
    gate: GateClient,
    acting: int,
    locale: str,
    target_id: int,
    actor_top: int | None,
    target_top: int | None,
) -> Reply:
    """First step of ``/unrank``: a confirmation card (the member must be ranked)."""
    me = await gate.staff_me(acting)
    target = await gate.get_staff(acting, target_id)
    if not can_act_on(
        actor_top,
        target_top,
        bool(me.get("is_admin")),
        bool(target.get("is_admin")),
        acting == target_id,
    ):
        return _hierarchy_card(locale, target_is_admin=bool(target.get("is_admin")))
    return card(
        t(locale, "unrank.confirm.title"),
        t(locale, "unrank.confirm.body", mention=_mention(target)),
        icon=emojis.WARNING,
        accent="warning",
        buttons=(
            Button(
                t(locale, "unrank.yes"),
                cid_unrank("yes", target_id),
                icon=emojis.REMOVE_MEMBER,
                style="danger",
            ),
            Button(t(locale, "unrank.no"), cid_unrank("no", target_id), icon=emojis.UNDONE),
        ),
    )


@guarded
async def unrank_do(
    gate: GateClient,
    acting: int,
    locale: str,
    target_id: int,
    actor_top: int | None,
    target_top: int | None,
) -> Reply:
    """Second step: the position is checked again (the click may come much later)."""
    me = await gate.staff_me(acting)
    target = await _lookup(gate, acting, target_id)
    target_is_admin = bool(target and target.get("is_admin"))
    if not can_act_on(
        actor_top, target_top, bool(me.get("is_admin")), target_is_admin, acting == target_id
    ):
        return _hierarchy_card(locale, target_is_admin=target_is_admin)
    await gate.unrank_staff(acting, target_id)
    return success(
        t(locale, "unrank.done.title"),
        t(locale, "unrank.done.body", mention=f"<@{target_id}>"),
    )


async def unrank_cancelled(gate: GateClient, acting: int, locale: str) -> Reply:
    return card(
        t(locale, "unrank.cancelled.title"),
        t(locale, "unrank.cancelled.body"),
        icon=emojis.UNDONE,
        accent="neutral",
    )


# --- Permissions panel ------------------------------------------------------------
def _panel(
    locale: str,
    *,
    me: Mapping[str, Any],
    target: Mapping[str, Any],
    acting: int,
    actor_top: int | None,
    target_top: int | None,
    notice: str = "",
) -> Reply:
    target_id = int(target["discord_id"])
    actor_is_admin = bool(me.get("is_admin"))
    actor_perms: Collection[str] = me.get("actor_permissions", ())
    target_is_admin = bool(target.get("is_admin"))
    required = PERM_MANAGE_SELF if acting == target_id else PERM_MANAGE_OTHERS
    hierarchy_ok = can_act_on(
        actor_top, target_top, actor_is_admin, target_is_admin, acting == target_id
    )
    can_edit = required in actor_perms and hierarchy_ok and not target_is_admin
    granted = frozenset(target.get("effective_permissions", ())) & ALL_PERMISSIONS
    label = {p: t(locale, f"team.perm.{p}.label") for p in ALL_PERMISSIONS}

    fields: list[Field] = []
    locked: list[str] = []
    removable = False
    for cat in CATEGORIES:
        name = f"{cat.icon} {t(locale, f'team.cat.{cat.id}')}".strip()
        offered = (
            cat.permissions
            if not can_edit
            else editable_permissions(actor_perms, actor_is_admin, cat)
        )
        held_locked = [p for p in cat.permissions if p in granted and p not in offered]
        removable = removable or bool(granted & set(offered))
        if not offered:
            if held_locked:
                locked.append(f"**{name}**\n-# {', '.join(label[p] for p in held_locked)}")
            continue
        options = tuple(
            Option(
                p,
                label[p],
                description=t(locale, f"team.perm.{p}.desc"),
                default=p in granted,
            )
            for p in offered
        )
        fields.append(
            Field(
                name,
                Select(
                    cid_perm_set(target_id, cat.id),
                    placeholder=t(locale, "perm.select.placeholder"),
                    options=options,
                    min_values=0,
                    max_values=len(options),
                    disabled=not can_edit,
                ),
                help=t(locale, "perm.locked", labels=", ".join(label[p] for p in held_locked))
                if held_locked
                else "",
            )
        )

    lines = [member_header(locale, target)]
    if target_is_admin:
        lines.append(t(locale, "perm.admin"))
    elif not can_edit:
        lines.append(t(locale, "perm.readonly"))
    return card(
        t(locale, "perm.title"),
        "\n".join(lines),
        icon=emojis.PERMISSIONS,
        breadcrumb=(t(locale, "team.title"), _name(locale, target)),
        details=((t(locale, "perm.granted"), code(f"{len(granted)}/{len(ALL_PERMISSIONS)}")),),
        items=tuple(locked),
        fields=tuple(fields),
        footer=notice,
        buttons=(
            Button(t(locale, "nav.back"), CID_TEAM_LIST, icon=emojis.BACK),
            Button(
                t(locale, "perm.clear"),
                cid_perm_clear("ask", target_id),
                icon=emojis.DELETE,
                style="danger",
                disabled=not (can_edit and removable),
            ),
        ),
    )


async def _fetch(
    gate: GateClient, acting: int, target_id: int
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    me, target = await asyncio.gather(gate.staff_me(acting), gate.get_staff(acting, target_id))
    return me, target


@guarded
async def permissions_panel(
    gate: GateClient,
    acting: int,
    locale: str,
    target_id: int,
    actor_top: int | None,
    target_top: int | None,
    *,
    confirm_clear: bool = False,
) -> Reply:
    """The panel of ``target_id`` (read-only when the actor may not edit), or the
    in-place confirmation of "Remove all"."""
    me, target = await _fetch(gate, acting, target_id)
    if confirm_clear:
        return card(
            t(locale, "perm.clear.title"),
            t(locale, "perm.clear.body", mention=_mention(target)),
            icon=emojis.WARNING,
            accent="warning",
            breadcrumb=(t(locale, "team.title"), _name(locale, target)),
            buttons=(
                Button(
                    t(locale, "perm.clear.yes"),
                    cid_perm_clear("yes", target_id),
                    icon=emojis.DELETE,
                    style="danger",
                ),
                Button(
                    t(locale, "perm.clear.no"), cid_perm_clear("no", target_id), icon=emojis.UNDONE
                ),
            ),
        )
    return _panel(
        locale, me=me, target=target, acting=acting, actor_top=actor_top, target_top=target_top
    )


async def _write(
    gate: GateClient,
    acting: int,
    locale: str,
    target_id: int,
    actor_top: int | None,
    target_top: int | None,
    compute: Any,
) -> Reply:
    me, target = await _fetch(gate, acting, target_id)
    actor_is_admin = bool(me.get("is_admin"))
    target_is_admin = bool(target.get("is_admin"))
    if not can_act_on(actor_top, target_top, actor_is_admin, target_is_admin, acting == target_id):
        return _hierarchy_card(locale, target_is_admin=target_is_admin)
    current = frozenset(target.get("effective_permissions", ())) & ALL_PERMISSIONS
    new = compute(me.get("actor_permissions", ()), actor_is_admin, current)
    notice = ""
    if new is not None and new != current:
        target = await gate.set_staff_permissions(acting, target_id, sorted(new))
        notice = t(locale, "perm.saved")
    return _panel(
        locale,
        me=me,
        target=target,
        acting=acting,
        actor_top=actor_top,
        target_top=target_top,
        notice=notice,
    )


@guarded
async def set_category(
    gate: GateClient,
    acting: int,
    locale: str,
    target_id: int,
    category_id: str,
    selected: Sequence[str],
    actor_top: int | None,
    target_top: int | None,
) -> Reply:
    """A category select was submitted: ``new = (current - options) | selected``."""
    category = _category(category_id)
    if category is None:
        return error_card(t(locale, "perm.title"), t(locale, "perm.forged"), severity="warning")
    cat = category

    def compute(
        actor_perms: Collection[str], actor_is_admin: bool, current: frozenset[str]
    ) -> frozenset[str]:
        options = editable_permissions(actor_perms, actor_is_admin, cat)
        return new_permissions(current, options, selected)

    return await _write(gate, acting, locale, target_id, actor_top, target_top, compute)


@guarded
async def clear_permissions(
    gate: GateClient,
    acting: int,
    locale: str,
    target_id: int,
    actor_top: int | None,
    target_top: int | None,
) -> Reply:
    """ "Remove all": removes every permission the actor is allowed to change."""

    def compute(
        actor_perms: Collection[str], actor_is_admin: bool, current: frozenset[str]
    ) -> frozenset[str]:
        options = [
            p for cat in CATEGORIES for p in editable_permissions(actor_perms, actor_is_admin, cat)
        ]
        return new_permissions(current, options, ())

    return await _write(gate, acting, locale, target_id, actor_top, target_top, compute)


# --- Help ---------------------------------------------------------------------------
@guarded
async def help_card(gate: GateClient, acting: int, locale: str) -> Reply:
    """Commands grouped by area, only those the member can use."""
    me = await gate.staff_me(acting)
    permissions: Collection[str] = me.get("actor_permissions", ())
    sections: list[str] = []
    for area, commands in COMMAND_AREAS:
        lines = [
            t(
                locale,
                "help.line",
                command=command,
                description=t(locale, f"help.cmd.{command}"),
            )
            for command in commands
            if command_allowed(command, permissions)
        ]
        if lines:
            sections.append(f"**{t(locale, f'help.area.{area}')}**\n" + "\n".join(lines))
    if not sections:
        return card(t(locale, "help.title"), t(locale, "help.empty"), icon=emojis.COMMANDS)
    return card(t(locale, "help.title"), icon=emojis.COMMANDS, items=tuple(sections))
