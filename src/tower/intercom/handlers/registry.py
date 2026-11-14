"""``/registry`` commands: reliability registry of sources (gate client only).

A tier or a controller is always changed with a reason: the tier rests on the editorial
process, never on political orientation. Gate decides the permissions.
"""

from __future__ import annotations

from tower.intercom.gate_client import GateClient
from tower.intercom.handlers import Reply, fmt_dt, guarded, listing, ok
from tower.intercom.i18n import t

__all__ = [
    "registry_history",
    "registry_seed_control",
    "registry_set_control",
    "registry_set_tier",
    "registry_show",
]

_NONE_WORDS = frozenset({"none", "aucun", "-"})
_HISTORY_LINES = 10


def _domain(raw: str) -> str:
    """``https://www.Example.org/page`` -> ``example.org`` (the gate normalizes again)."""
    value = raw.strip().lower().split("://", 1)[-1].split("/", 1)[0].split("?", 1)[0]
    return value.removeprefix("www.")


def _entry_text(locale: str, e: dict[str, object]) -> str:
    lines = [
        t(
            locale,
            "registry.show",
            domain=e["domain"],
            publisher=e["publisher"],
            tier=e["tier"],
            kind=e["kind"],
            country=e.get("country") or "-",
            version=e["version"],
        )
    ]
    if e.get("reason"):
        lines.append(t(locale, "registry.show.reason", reason=e["reason"]))
    if e.get("controlled_by"):
        lines.append(
            t(
                locale,
                "registry.show.control",
                controlled_by=e["controlled_by"],
                origin=e.get("control_origin") or "-",
            )
        )
    else:
        lines.append(t(locale, "registry.show.no_control"))
    if e.get("statutory_independence"):
        lines.append(t(locale, "registry.show.independence", basis=e.get("independence_basis")))
    if not e.get("active", True):
        lines.append(t(locale, "registry.show.inactive"))
    return "\n".join(lines)


@guarded
async def registry_show(gate: GateClient, acting: int, locale: str, domain: str) -> Reply:
    return ok(_entry_text(locale, await gate.get_registry_entry(acting, _domain(domain))))


@guarded
async def registry_set_tier(
    gate: GateClient, acting: int, locale: str, domain: str, tier: str, reason: str
) -> Reply:
    entry = await gate.get_registry_entry(acting, _domain(domain))
    done = await gate.update_registry_entry(
        acting,
        str(entry["id"]),
        version=int(entry["version"]),
        reason=reason,
        changes={"tier": tier.strip().lower()},
    )
    return ok(
        t(
            locale,
            "registry.tier_set",
            domain=done["domain"],
            tier=done["tier"],
            version=done["version"],
        )
    )


@guarded
async def registry_set_control(
    gate: GateClient, acting: int, locale: str, domain: str, controlled_by: str, reason: str
) -> Reply:
    value = controlled_by.strip()
    clearing = value.lower() in _NONE_WORDS
    entry = await gate.get_registry_entry(acting, _domain(domain))
    done = await gate.update_registry_entry(
        acting,
        str(entry["id"]),
        version=int(entry["version"]),
        reason=reason,
        changes={"controlled_by": None if clearing else value},
    )
    key = "registry.control_cleared" if clearing else "registry.control_set"
    return ok(
        t(
            locale,
            key,
            domain=done["domain"],
            controlled_by=done.get("controlled_by") or "-",
            version=done["version"],
        )
    )


@guarded
async def registry_history(gate: GateClient, acting: int, locale: str, domain: str) -> Reply:
    rows = await gate.registry_history(acting, _domain(domain))
    if not rows:
        return ok(t(locale, "registry.history.empty"))
    lines = [
        t(
            locale,
            "registry.history.line",
            version=r["version"],
            change=r["change"],
            by=r["changed_by"],
            when=fmt_dt(r["created_at"]),
            reason=r.get("reason") or "-",
        )
        for r in rows[:_HISTORY_LINES]
    ]
    return ok(
        listing(locale, t(locale, "registry.history.header", domain=domain, count=len(rows)), lines)
    )


@guarded
async def registry_seed_control(gate: GateClient, acting: int, locale: str, domain: str) -> Reply:
    done = await gate.seed_registry_control(acting, _domain(domain))
    key = "registry.seed_queued" if done.get("enqueued") else "registry.seed_pending"
    return ok(t(locale, key, domain=domain))
