"""``/usage`` card (LLM free allocation and spend)."""

from __future__ import annotations

from tower.intercom.gate_client import GateClient
from tower.intercom.handlers import Button, Reply, card, code, fmt_usd, guarded
from tower.intercom.i18n import t
from tower.intercom.ui import emojis

__all__ = ["CID_REFRESH", "show_usage"]

CID_REFRESH = "t12a:use:refresh"


@guarded
async def show_usage(gate: GateClient, acting: int, locale: str) -> Reply:
    u = await gate.get_usage(acting)
    free_lines: list[str] = []
    for f in u.get("free", []):
        allowance = int(f["allowance_tokens"])
        name = code(f"{f['provider']}/{f['free_group']}")
        if allowance <= 0:
            free_lines.append(t(locale, "usage.free.unavailable", name=name))
            continue
        used = int(f["used_tokens"])
        free_lines.append(
            t(
                locale,
                "usage.free.line",
                name=name,
                pct=f"{used * 100 / allowance:.0f}",
                used=used,
                allowance=allowance,
            )
        )
    spend_lines = [
        t(
            locale,
            "usage.spend.line",
            scope=code(s["scope"]),
            spent=fmt_usd(s["spent_usd_micros"]),
            cap=fmt_usd(s["cap_usd_micros"]),
        )
        for s in u.get("scopes", [])
    ] or [t(locale, "usage.spend.empty")]
    items = (
        "\n".join([t(locale, "usage.free.title"), *(free_lines or [t(locale, "usage.free.none")])]),
        "\n".join([t(locale, "usage.spend.title"), *spend_lines]),
    )
    return card(
        t(locale, "usage.title"),
        icon=emojis.ANALYTICS,
        details=(
            (t(locale, "usage.d.day"), code(u.get("day", "-"))),
            (
                t(locale, "usage.d.period"),
                code(f"{u.get('period_start', '-')} -> {u.get('period_end', '-')}"),
            ),
        ),
        items=items,
        buttons=(Button(t(locale, "ops.refresh"), CID_REFRESH, emojis.SYNC),),
    )
