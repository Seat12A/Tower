"""``/incidents`` commands (stored errors)."""

from __future__ import annotations

from tower.intercom.gate_client import GateClient
from tower.intercom.handlers import Reply, fmt_dt, guarded, listing, ok, short
from tower.intercom.i18n import t

__all__ = ["list_incidents", "resolve"]

_MIN_PREFIX = 6


@guarded
async def list_incidents(gate: GateClient, acting: int, locale: str, limit: int = 10) -> Reply:
    rows = await gate.list_incidents(acting, limit=limit)
    if not rows:
        return ok(t(locale, "incidents.list.empty"))
    lines = [
        t(
            locale,
            "incidents.list.line",
            code=r["code"],
            title=r.get("title") or "",
            count=r.get("occurrences", 1),
            last_seen=fmt_dt(r.get("last_seen")),
            short_fp=short(r["fingerprint"], 12),
        )
        for r in rows
    ]
    return ok(listing(locale, t(locale, "incidents.list.header", count=len(rows)), lines))


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
            return ok(t(locale, "common.ambiguous", ref=ref))
        if matches:
            target = str(matches[0])
    resolved = await gate.resolve_incident(acting, target, note=note)
    return ok(
        t(
            locale,
            "incidents.resolved",
            short_fp=short(resolved["fingerprint"], 12),
            code=resolved["code"],
        )
    )
