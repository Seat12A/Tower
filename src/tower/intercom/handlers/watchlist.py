"""``/watchlist`` commands."""

from __future__ import annotations

from typing import Any

from tower.intercom.gate_client import GateClient, GateError
from tower.intercom.handlers import Reply, guarded, listing, ok, short
from tower.intercom.i18n import t

__all__ = ["add", "deactivate", "edit", "list_accounts"]

_MIN_PREFIX = 6


def _account(row: dict[str, Any]) -> str:
    if row.get("handle"):
        return f"@{row['handle']}"
    return str(row.get("external_id") or "?")


@guarded
async def list_accounts(
    gate: GateClient, acting: int, locale: str, platform: str | None = None
) -> Reply:
    rows = await gate.list_watched(acting, platform=platform, limit=100)
    if not rows:
        return ok(t(locale, "watchlist.list.empty"))
    lines = [
        t(
            locale,
            "watchlist.list.line",
            platform=r["platform"],
            account=_account(r),
            category=r["category"],
            priority=r["priority"],
            short_id=short(r["public_id"]),
            pending=t(locale, "watchlist.pending") if r.get("external_id") is None else "",
        )
        for r in rows
    ]
    return ok(listing(locale, t(locale, "watchlist.list.header", count=len(rows)), lines))


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
        note=note,
    )
    return ok(
        t(
            locale,
            "watchlist.added",
            platform=created["platform"],
            account=_account(created),
            category=created["category"],
            priority=created["priority"],
            short_id=short(created["public_id"]),
        )
    )


async def _find(gate: GateClient, acting: int, ref: str) -> dict[str, Any]:
    """Account by full public id or unambiguous prefix (the PATCH needs ``version``, which
    only the list/read side gives; gate has no GET by id for watched accounts)."""
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


async def _update(
    gate: GateClient, acting: int, locale: str, ref: str, key: str, **changes: Any
) -> Reply:
    try:
        row = await _find(gate, acting, ref)
    except _Ambiguous as exc:
        return ok(t(locale, "common.ambiguous", ref=exc.ref))
    updated = await gate.update_watched(
        acting, str(row["public_id"]), version=int(row["version"]), **changes
    )
    return ok(
        t(
            locale,
            key,
            platform=updated["platform"],
            account=_account(updated),
            short_id=short(updated["public_id"]),
        )
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
        return ok(t(locale, "watchlist.nothing_to_change"))
    return await _update(gate, acting, locale, public_id, "watchlist.updated", **changes)


@guarded
async def deactivate(gate: GateClient, acting: int, locale: str, public_id: str) -> Reply:
    return await _update(gate, acting, locale, public_id, "watchlist.deactivated", active=False)
