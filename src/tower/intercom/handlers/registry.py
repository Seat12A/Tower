"""``/registry`` cards: reliability registry of sources (gate client only).

A tier or a controller is always changed with a reason: the tier rests on the editorial
process, never on political orientation. Gate decides the permissions.
"""

from __future__ import annotations

import re
from typing import Any

from tower.intercom.gate_client import GateClient
from tower.intercom.handlers import (
    Button,
    Reply,
    card,
    code,
    fmt_dt,
    guarded,
    listing,
    success,
)
from tower.intercom.i18n import t
from tower.intercom.ui import emojis

__all__ = [
    "CID_MAX",
    "TIERS",
    "cid",
    "normalize_domain",
    "registry_history",
    "registry_seed_control",
    "registry_set_control",
    "registry_set_tier",
    "registry_show",
]

TIERS = ("primary", "secondary", "tertiary", "avoid")
CID_MAX = 100
_CID_DOMAIN = re.compile(r"[0-9a-z.-]{1,80}")
_NONE_WORDS = frozenset({"none", "aucun", "-"})
_HISTORY_LINES = 10


def normalize_domain(raw: str) -> str:
    """``https://www.Example.org/page`` -> ``example.org`` (the gate normalizes again)."""
    value = raw.strip().lower().split("://", 1)[-1].split("/", 1)[0].split("?", 1)[0]
    return value.removeprefix("www.")


def cid(action: str, domain: str) -> str:
    """Custom id ``t12a:reg:<action>:<domain>``; empty when it would exceed Discord's limit."""
    if not _CID_DOMAIN.fullmatch(domain):
        return ""
    value = f"t12a:reg:{action}:{domain}"
    return value if len(value) <= CID_MAX else ""


def _buttons(locale: str, domain: str, *, show: bool = False) -> tuple[Button, ...]:
    """Action bar of an entry; a button is left out when the domain is too long for an id."""
    specs = [
        ("show", t(locale, "registry.btn.show"), emojis.BOOK, "secondary"),
        ("tier", t(locale, "registry.btn.tier"), emojis.EDIT, "primary"),
        ("control", t(locale, "registry.btn.control"), emojis.EDIT, "primary"),
        ("history", t(locale, "registry.btn.history"), emojis.HISTORY, "secondary"),
        ("seed", t(locale, "registry.btn.seed"), emojis.WEB, "secondary"),
    ]
    return tuple(
        Button(label, cid(action, domain), icon, style)  # type: ignore[arg-type]
        for action, label, icon, style in specs
        if (show or action != "show") and cid(action, domain)
    )


def _entry_card(locale: str, e: dict[str, Any], footer: str = "") -> Reply:
    details = [
        (t(locale, "registry.d.publisher"), code(e["publisher"])),
        (t(locale, "registry.d.tier"), code(e["tier"])),
        (t(locale, "registry.d.kind"), code(e["kind"])),
        (t(locale, "registry.d.country"), code(e.get("country") or "-")),
        (t(locale, "ops.d.version"), code(f"v{e['version']}")),
    ]
    if e.get("controlled_by"):
        details.append(
            (
                t(locale, "registry.d.control"),
                t(
                    locale,
                    "registry.show.control",
                    controlled_by=code(e["controlled_by"]),
                    origin=code(e.get("control_origin") or "-"),
                ),
            )
        )
    else:
        details.append((t(locale, "registry.d.control"), t(locale, "registry.show.no_control")))
    if e.get("statutory_independence"):
        details.append(
            (
                t(locale, "registry.d.independence"),
                t(locale, "registry.show.independence", basis=code(e.get("independence_basis"))),
            )
        )
    notes: list[str] = []
    if e.get("reason"):
        notes.append(t(locale, "registry.show.reason", reason=e["reason"]))
    if not e.get("active", True):
        notes.append(t(locale, "registry.show.inactive"))
    return card(
        str(e["domain"]),
        "\n".join(notes),
        icon=emojis.BOOK,
        accent="primary" if e.get("active", True) else "neutral",
        details=tuple(details),
        footer=footer,
        buttons=_buttons(locale, str(e["domain"])),
    )


@guarded
async def registry_show(gate: GateClient, acting: int, locale: str, domain: str) -> Reply:
    return _entry_card(locale, await gate.get_registry_entry(acting, normalize_domain(domain)))


async def _update(
    gate: GateClient,
    acting: int,
    locale: str,
    domain: str,
    reason: str,
    changes: dict[str, Any],
) -> dict[str, Any]:
    entry = await gate.get_registry_entry(acting, normalize_domain(domain))
    return await gate.update_registry_entry(
        acting, str(entry["id"]), version=int(entry["version"]), reason=reason, changes=changes
    )


@guarded
async def registry_set_tier(
    gate: GateClient, acting: int, locale: str, domain: str, tier: str, reason: str
) -> Reply:
    done = await _update(gate, acting, locale, domain, reason, {"tier": tier.strip().lower()})
    return _entry_card(
        locale,
        done,
        t(
            locale,
            "registry.tier_set",
            domain=done["domain"],
            tier=done["tier"],
            version=done["version"],
        ),
    )


@guarded
async def registry_set_control(
    gate: GateClient, acting: int, locale: str, domain: str, controlled_by: str, reason: str
) -> Reply:
    value = controlled_by.strip()
    clearing = value.lower() in _NONE_WORDS
    done = await _update(
        gate, acting, locale, domain, reason, {"controlled_by": None if clearing else value}
    )
    key = "registry.control_cleared" if clearing else "registry.control_set"
    return _entry_card(
        locale,
        done,
        t(
            locale,
            key,
            domain=done["domain"],
            controlled_by=done.get("controlled_by") or "-",
            version=done["version"],
        ),
    )


@guarded
async def registry_history(gate: GateClient, acting: int, locale: str, domain: str) -> Reply:
    name = normalize_domain(domain)
    rows = await gate.registry_history(acting, name)
    back = (
        (Button(t(locale, "ops.back"), cid("show", name), emojis.BACK),)
        if cid("show", name)
        else ()
    )
    if not rows:
        return card(
            t(locale, "registry.history.title"),
            t(locale, "registry.history.empty"),
            icon=emojis.HISTORY,
            accent="neutral",
            breadcrumb=(t(locale, "registry.title"), name),
            buttons=back,
        )
    lines = [
        t(
            locale,
            "registry.history.item",
            version=code(f"v{r['version']}"),
            change=r["change"],
            by=code(r["changed_by"]),
            when=fmt_dt(r["created_at"]),
            reason=r.get("reason") or "-",
        )
        for r in rows[:_HISTORY_LINES]
    ]
    items, more = listing(locale, lines)
    return card(
        t(locale, "registry.history.title"),
        icon=emojis.HISTORY,
        breadcrumb=(t(locale, "registry.title"), name),
        items=items,
        more=more
        or (
            t(locale, "common.more", count=len(rows) - _HISTORY_LINES)
            if len(rows) > _HISTORY_LINES
            else ""
        ),
        buttons=back,
    )


@guarded
async def registry_seed_control(gate: GateClient, acting: int, locale: str, domain: str) -> Reply:
    name = normalize_domain(domain)
    done = await gate.seed_registry_control(acting, name)
    key = "registry.seed_queued" if done.get("enqueued") else "registry.seed_pending"
    back = (
        (Button(t(locale, "ops.back"), cid("show", name), emojis.BACK),)
        if cid("show", name)
        else ()
    )
    if done.get("enqueued"):
        return success(t(locale, "registry.seed.title"), t(locale, key, domain=name), buttons=back)
    return card(
        t(locale, "registry.seed.title"),
        t(locale, key, domain=name),
        icon=emojis.INFO,
        buttons=back,
    )
