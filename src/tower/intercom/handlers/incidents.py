"""``/incidents`` cards (stored errors): list with a resolve select, resolve with a note."""

from __future__ import annotations

from tower.intercom.gate_client import GateClient
from tower.intercom.handlers import (
    Button,
    Field,
    Option,
    Reply,
    Select,
    card,
    code,
    error_card,
    fmt_dt,
    guarded,
    listing,
    short,
    success,
)
from tower.intercom.i18n import t
from tower.intercom.ui import emojis

__all__ = ["CID_LIST", "CID_PICK", "list_incidents", "resolve"]

CID_LIST = "t12a:inc:list"
CID_PICK = "t12a:inc:pick"
_MIN_PREFIX = 6
_SELECT_MAX = 25


@guarded
async def list_incidents(gate: GateClient, acting: int, locale: str, limit: int = 10) -> Reply:
    rows = await gate.list_incidents(acting, limit=limit)
    refresh = Button(t(locale, "ops.refresh"), CID_LIST, emojis.SYNC)
    if not rows:
        return card(
            t(locale, "incidents.title"),
            t(locale, "incidents.list.empty"),
            icon=emojis.WARNING,
            accent="neutral",
            buttons=(refresh,),
        )
    lines = [
        t(
            locale,
            "incidents.item",
            code=code(r["code"]),
            title=r.get("title") or "",
            count=r.get("occurrences", 1),
            last_seen=fmt_dt(r.get("last_seen")),
            short_fp=code(short(r["fingerprint"], 12)),
        )
        for r in rows
    ]
    items, more = listing(locale, lines)
    return card(
        t(locale, "incidents.title"),
        icon=emojis.WARNING,
        items=items,
        more=more,
        fields=(
            Field(
                t(locale, "incidents.resolve.label"),
                Select(
                    CID_PICK,
                    placeholder=t(locale, "incidents.resolve.placeholder"),
                    options=tuple(
                        Option(
                            str(r["fingerprint"])[:100],
                            str(r["code"]),
                            description=str(r.get("title") or "")[:100],
                        )
                        for r in rows[:_SELECT_MAX]
                    ),
                ),
                help=t(locale, "incidents.resolve.help"),
            ),
        ),
        buttons=(refresh,),
    )


@guarded
async def resolve(
    gate: GateClient, acting: int, locale: str, fingerprint: str, note: str | None = None
) -> Reply:
    ref = fingerprint.strip().lower()
    target = ref
    if len(ref) >= _MIN_PREFIX:
        open_rows = await gate.list_incidents(acting, limit=200)
        matches = [r["fingerprint"] for r in open_rows if str(r["fingerprint"]).startswith(ref)]
        if len(matches) > 1:
            return error_card(
                t(locale, "common.ambiguous.title"),
                t(locale, "common.ambiguous", ref=ref),
                severity="warning",
            )
        if matches:
            target = str(matches[0])
    resolved = await gate.resolve_incident(acting, target, note=(note or "").strip() or None)
    return success(
        t(locale, "incidents.resolved.title"),
        t(
            locale,
            "incidents.resolved",
            short_fp=short(resolved["fingerprint"], 12),
            code=resolved["code"],
        ),
        buttons=(Button(t(locale, "incidents.btn.back"), CID_LIST, emojis.BACK),),
    )
