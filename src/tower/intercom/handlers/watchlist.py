"""``/watchlist`` cards: paged list with a platform filter, account card, add/edit/deactivate."""

from __future__ import annotations

from typing import Any

from tower.core.domain.platforms import Platform
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
    short,
    success,
)
from tower.intercom.i18n import t
from tower.intercom.ui import emojis

__all__ = [
    "PAGE_SIZE",
    "PLATFORMS",
    "add",
    "add_form",
    "cid",
    "confirm_deactivate",
    "deactivate",
    "edit",
    "edit_form",
    "list_accounts",
    "show_account",
]

PAGE_SIZE = 8
PLATFORMS = tuple(p.value for p in Platform)
ALL = "all"
_MIN_PREFIX = 6
_PRIORITY_MIN, _PRIORITY_MAX = -100, 100


def cid(action: str, *params: object) -> str:
    """Custom id of a watchlist component: ``t12a:wl:<action>[:<param>...]``."""
    return ":".join(["t12a", "wl", action, *(str(p) for p in params)])


def _account(row: dict[str, Any]) -> str:
    if row.get("handle"):
        return f"@{row['handle']}"
    return str(row.get("external_id") or "?")


def parse_priority(raw: str | int | None) -> int | None:
    """Priority from a form field: empty means 0; ``None`` when invalid or out of range."""
    if raw is None or raw == "":
        return 0
    try:
        value = int(str(raw).strip() or "0")
    except ValueError:
        return None
    return value if _PRIORITY_MIN <= value <= _PRIORITY_MAX else None


def _bad_priority(locale: str) -> Reply:
    return error_card(
        t(locale, "watchlist.bad_priority.title"),
        t(locale, "watchlist.bad_priority.body"),
        severity="warning",
    )


def _item(locale: str, r: dict[str, Any]) -> str:
    pending = t(locale, "watchlist.pending") if r.get("external_id") is None else ""
    inactive = "" if r.get("active", True) else t(locale, "watchlist.inactive")
    return t(
        locale,
        "watchlist.item",
        account=code(_account(r)),
        platform=r["platform"],
        category=r["category"],
        priority=r["priority"],
        short_id=code(short(r["public_id"])),
        flags=f"{pending}{inactive}",
    )


@guarded
async def list_accounts(
    gate: GateClient,
    acting: int,
    locale: str,
    platform: str | None = None,
    page: int = 0,
) -> Reply:
    platform = platform if platform in PLATFORMS else None
    rows = await gate.list_watched(acting, platform=platform, limit=100)
    pages = max(1, -(-len(rows) // PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    shown = rows[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    current = platform or ALL
    fields = [
        Field(
            t(locale, "watchlist.filter.label"),
            Select(
                cid("plat"),
                placeholder=t(locale, "watchlist.filter.label"),
                options=(
                    Option(ALL, t(locale, "watchlist.filter.all"), default=current == ALL),
                    *(Option(p, p, default=current == p) for p in PLATFORMS),
                ),
            ),
        )
    ]
    if shown:
        fields.append(
            Field(
                t(locale, "watchlist.open.label"),
                Select(
                    cid("open"),
                    placeholder=t(locale, "watchlist.open.placeholder"),
                    options=tuple(
                        Option(
                            str(r["public_id"]),
                            _account(r),
                            description=f"{r['platform']} · {r['category']}",
                        )
                        for r in shown
                    ),
                ),
                help=t(locale, "watchlist.open.help"),
            )
        )
    return card(
        t(locale, "watchlist.title"),
        "" if shown else t(locale, "watchlist.list.empty"),
        icon=emojis.TRACK,
        accent="primary" if shown else "neutral",
        items=tuple(_item(locale, r) for r in shown),
        fields=tuple(fields),
        footer=t(locale, "watchlist.page", page=page + 1, pages=pages) if pages > 1 else "",
        buttons=(
            Button(
                t(locale, "ops.prev"),
                cid("page", current, page - 1),
                emojis.BACK,
                disabled=page == 0,
            ),
            Button(
                t(locale, "ops.next"),
                cid("page", current, page + 1),
                emojis.NEXT,
                disabled=page >= pages - 1,
            ),
            Button(t(locale, "watchlist.btn.add"), cid("add"), emojis.ADD, "primary"),
        ),
    )


def _list_button(locale: str, platform: str = ALL) -> Button:
    return Button(t(locale, "watchlist.btn.back"), cid("page", platform, 0), emojis.BACK)


@guarded
async def add(
    gate: GateClient,
    acting: int,
    locale: str,
    platform: str,
    handle: str,
    category: str,
    priority: int = 0,
    note: str | None = None,
) -> Reply:
    created = await gate.add_watched(
        acting,
        platform=platform,
        handle=handle.strip().removeprefix("@"),
        category=category,
        priority=priority,
        note=note or None,
    )
    return success(
        t(locale, "watchlist.added.title"),
        t(locale, "watchlist.added.body", account=code(_account(created))),
        details=(
            (t(locale, "ops.d.platform"), code(created["platform"])),
            (t(locale, "ops.d.category"), code(created["category"])),
            (t(locale, "ops.d.priority"), code(created["priority"])),
            (t(locale, "ops.d.id"), code(short(created["public_id"]))),
        ),
        buttons=(
            Button(
                t(locale, "watchlist.btn.open"), cid("show", created["public_id"]), emojis.TRACK
            ),
            _list_button(locale, created["platform"]),
        ),
    )


@guarded
async def add_form(
    gate: GateClient,
    acting: int,
    locale: str,
    platform: str,
    handle: str,
    category: str,
    priority_raw: str = "",
    note: str | None = None,
) -> Reply:
    """``/watchlist add`` modal: the priority arrives as typed text."""
    priority = parse_priority(priority_raw)
    if priority is None:
        return _bad_priority(locale)
    return await add(gate, acting, locale, platform, handle, category, priority, note)


async def _find(gate: GateClient, acting: int, ref: str) -> dict[str, Any]:
    """Account by full public id or unambiguous prefix (the PATCH needs ``version``, which
    only the list side gives; gate has no GET by id for watched accounts)."""
    ref = ref.strip().lower()
    if len(ref) < _MIN_PREFIX:
        raise GateError(422, "TWR-GEN-001", f"Identifier too short (min {_MIN_PREFIX} characters)")
    rows = await gate.list_watched(acting, active=None, limit=500)
    matches = [r for r in rows if str(r["public_id"]).lower().startswith(ref)]
    if not matches:
        raise GateError(404, "TWR-ING-008", "Watched account not found")
    if len(matches) > 1:
        raise _Ambiguous(ref)
    return matches[0]


class _Ambiguous(Exception):
    def __init__(self, ref: str) -> None:
        super().__init__(ref)
        self.ref = ref


def _ambiguous(locale: str, exc: _Ambiguous) -> Reply:
    return error_card(
        t(locale, "common.ambiguous.title"),
        t(locale, "common.ambiguous", ref=exc.ref),
        severity="warning",
    )


@guarded
async def show_account(gate: GateClient, acting: int, locale: str, ref: str) -> Reply:
    try:
        row = await _find(gate, acting, ref)
    except _Ambiguous as exc:
        return _ambiguous(locale, exc)
    active = row.get("active", True)
    details = [
        (t(locale, "ops.d.platform"), code(row["platform"])),
        (t(locale, "ops.d.category"), code(row["category"])),
        (t(locale, "ops.d.priority"), code(row["priority"])),
        (t(locale, "ops.d.status"), code(t(locale, "watchlist.active" if active else "ops.off"))),
        (t(locale, "ops.d.id"), code(short(row["public_id"]))),
    ]
    if row.get("note"):
        details.append((t(locale, "ops.d.note"), code(row["note"])))
    pending = t(locale, "watchlist.pending_line") if row.get("external_id") is None else ""
    return card(
        _account(row),
        pending,
        icon=emojis.TRACK,
        accent="primary" if active else "neutral",
        breadcrumb=(t(locale, "watchlist.title"),),
        details=tuple(details),
        buttons=(
            _list_button(locale, row["platform"]),
            Button(t(locale, "ops.edit"), cid("edit", row["public_id"]), emojis.EDIT),
            Button(
                t(locale, "watchlist.btn.deactivate"),
                cid("off", row["public_id"]),
                emojis.PAUSE,
                "danger",
                disabled=not active,
            ),
        ),
    )


@guarded
async def confirm_deactivate(gate: GateClient, acting: int, locale: str, ref: str) -> Reply:
    try:
        row = await _find(gate, acting, ref)
    except _Ambiguous as exc:
        return _ambiguous(locale, exc)
    return card(
        t(locale, "watchlist.confirm.title"),
        t(locale, "watchlist.confirm.body", account=code(_account(row))),
        icon=emojis.WARNING,
        accent="warning",
        breadcrumb=(t(locale, "watchlist.title"), _account(row)),
        buttons=(
            Button(t(locale, "ops.cancel"), cid("show", row["public_id"]), emojis.UNDONE),
            Button(
                t(locale, "watchlist.btn.deactivate"),
                cid("offyes", row["public_id"]),
                emojis.PAUSE,
                "danger",
            ),
        ),
    )


async def _update(
    gate: GateClient, acting: int, locale: str, ref: str, title_key: str, **changes: Any
) -> Reply:
    try:
        row = await _find(gate, acting, ref)
    except _Ambiguous as exc:
        return _ambiguous(locale, exc)
    updated = await gate.update_watched(
        acting, str(row["public_id"]), version=int(row["version"]), **changes
    )
    return success(
        t(locale, title_key),
        t(
            locale,
            "watchlist.updated.body",
            account=code(_account(updated)),
            platform=updated["platform"],
        ),
        details=((t(locale, "ops.d.id"), code(short(updated["public_id"]))),),
        buttons=(
            Button(
                t(locale, "watchlist.btn.open"), cid("show", updated["public_id"]), emojis.TRACK
            ),
            _list_button(locale, updated["platform"]),
        ),
    )


@guarded
async def edit(
    gate: GateClient,
    acting: int,
    locale: str,
    public_id: str,
    category: str | None = None,
    priority: int | None = None,
    note: str | None = None,
    handle: str | None = None,
) -> Reply:
    changes: dict[str, Any] = {}
    if category is not None:
        changes["category"] = category
    if priority is not None:
        changes["priority"] = priority
    if note is not None:
        changes["note"] = note
    if handle is not None:
        changes["handle"] = handle.strip().removeprefix("@")
    if not changes:
        return card(
            t(locale, "watchlist.nothing.title"),
            t(locale, "watchlist.nothing_to_change"),
            icon=emojis.INFO,
        )
    return await _update(gate, acting, locale, public_id, "watchlist.updated.title", **changes)


@guarded
async def edit_form(
    gate: GateClient,
    acting: int,
    locale: str,
    public_id: str,
    category: str | None = None,
    priority_raw: str = "",
    note: str = "",
    handle: str = "",
) -> Reply:
    """Edit modal: an empty field keeps the current value."""
    priority: int | None = None
    if priority_raw.strip():
        priority = parse_priority(priority_raw)
        if priority is None:
            return _bad_priority(locale)
    return await edit(
        gate,
        acting,
        locale,
        public_id,
        category or None,
        priority,
        note.strip() or None,
        handle.strip() or None,
    )


@guarded
async def deactivate(gate: GateClient, acting: int, locale: str, public_id: str) -> Reply:
    return await _update(
        gate, acting, locale, public_id, "watchlist.deactivated.title", active=False
    )
