"""``/usage`` command (LLM free allocation and spend)."""

from __future__ import annotations

from tower.intercom.gate_client import GateClient
from tower.intercom.handlers import Reply, fmt_usd, guarded, listing, ok
from tower.intercom.i18n import t

__all__ = ["show_usage"]


@guarded
async def show_usage(gate: GateClient, acting: int, locale: str) -> Reply:
    u = await gate.get_usage(acting)
    lines = [t(locale, "usage.free.header")]
    for f in u.get("free", []):
        allowance = int(f["allowance_tokens"])
        if allowance <= 0:
            lines.append(
                t(locale, "usage.free.unavailable", provider=f["provider"], group=f["free_group"])
            )
            continue
        used = int(f["used_tokens"])
        lines.append(
            t(
                locale,
                "usage.free.line",
                provider=f["provider"],
                group=f["free_group"],
                pct=f"{used * 100 / allowance:.0f}",
                used=used,
                allowance=allowance,
            )
        )
    lines.append(t(locale, "usage.spend.header"))
    scopes = u.get("scopes", [])
    if not scopes:
        lines.append(t(locale, "usage.spend.empty"))
    for s in scopes:
        lines.append(
            t(
                locale,
                "usage.spend.line",
                scope=s["scope"],
                spent=fmt_usd(s["spent_usd_micros"]),
                cap=fmt_usd(s["cap_usd_micros"]),
            )
        )
    header = t(
        locale,
        "usage.header",
        day=u.get("day", "-"),
        period_start=u.get("period_start", "-"),
        period_end=u.get("period_end", "-"),
    )
    return ok(listing(locale, header, lines))
