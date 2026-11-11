"""``/profile`` and ``/level`` commands (processing profiles, level overrides)."""

from __future__ import annotations

import json
from typing import Any

from tower.intercom.gate_client import GateClient, GateError
from tower.intercom.handlers import Reply, fmt_dt, guarded, listing, ok, short
from tower.intercom.i18n import t

__all__ = [
    "history",
    "level_list",
    "level_revoke",
    "level_set",
    "parse_value",
    "rollback",
    "set_value",
    "show",
]

_MIN_PREFIX = 6


def parse_value(raw: str) -> Any:
    """JSON value (``0.2``, ``true``, ``40``); anything else stays a string."""
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _yes_no(locale: str, value: object) -> str:
    return t(locale, "profile.yes" if value else "profile.no")


@guarded
async def show(gate: GateClient, acting: int, locale: str) -> Reply:
    active = await gate.get_active_profile(acting)
    cfg = active["settings"]
    version = int(active["version"])
    lines = [
        t(
            locale,
            "profile.show.header",
            version=version,
            origin=t(locale, "profile.show.default" if version == 0 else "profile.show.stored"),
        ),
        t(
            locale,
            "profile.show.general",
            same=cfg["match_same_threshold"],
            grey=cfg["match_grey_threshold"],
            quota=cfg["draft_daily_quota"],
            sample=cfg["e1_sample_rate"],
        ),
    ]
    for level, s in cfg["levels"].items():
        lines.append(
            t(
                locale,
                "profile.show.level",
                level=level,
                e1=s["e1_threshold"],
                reuse=_yes_no(locale, s["direct_reuse"]),
                cap=s["unit_cap_usd"],
                budget=s["monthly_budget_usd"],
            )
        )
    return ok("\n".join(lines))


@guarded
async def history(gate: GateClient, acting: int, locale: str) -> Reply:
    rows = await gate.list_profiles(acting)
    lines = [
        t(
            locale,
            "profile.history.line",
            version=r["version"],
            active=t(locale, "profile.history.active") if r["active"] else "",
            author=r["author"],
            created=fmt_dt(r.get("created_at")),
            reason=r["reason"],
        )
        for r in rows
    ]
    return ok(listing(locale, t(locale, "profile.history.header", count=len(rows)), lines))


@guarded
async def set_value(
    gate: GateClient, acting: int, locale: str, path: str, value: str, reason: str
) -> Reply:
    parsed = parse_value(value.strip())
    created = await gate.create_profile(
        acting, changes={path.strip(): parsed}, reason=reason, activate=True
    )
    return ok(
        t(
            locale,
            "profile.set.done",
            version=created["version"],
            path=path.strip(),
            value=json.dumps(parsed),
        )
    )


@guarded
async def rollback(gate: GateClient, acting: int, locale: str, reason: str) -> Reply:
    profile = await gate.rollback_profile(acting, reason=reason)
    return ok(t(locale, "profile.rollback.done", version=profile["version"]))


@guarded
async def level_set(
    gate: GateClient,
    acting: int,
    locale: str,
    target_type: str,
    target: str,
    level: str,
    hours: float,
    reason: str,
) -> Reply:
    created = await gate.create_level_override(
        acting,
        target_type=target_type,
        target=target.strip(),
        level=level,
        hours=hours,
        reason=reason,
    )
    return ok(
        t(
            locale,
            "level.set.done",
            level=created["level"],
            target_type=created["target_type"],
            target=created["target"],
            expires=fmt_dt(created["expires_at"]),
            short_id=short(created["id"]),
            applied=t(
                locale, "level.set.applied" if created.get("applied") else "level.set.not_applied"
            ),
        )
    )


@guarded
async def level_list(gate: GateClient, acting: int, locale: str) -> Reply:
    rows = await gate.list_level_overrides(acting, active=True, limit=100)
    if not rows:
        return ok(t(locale, "level.list.empty"))
    lines = [
        t(
            locale,
            "level.list.line",
            short_id=short(r["id"]),
            target_type=r["target_type"],
            target=r["target"],
            level=r["level"],
            expires=fmt_dt(r["expires_at"]),
            reason=r["reason"],
        )
        for r in rows
    ]
    return ok(listing(locale, t(locale, "level.list.header", count=len(rows)), lines))


@guarded
async def level_revoke(
    gate: GateClient, acting: int, locale: str, override_id: str, reason: str
) -> Reply:
    ref = override_id.strip().lower()
    if len(ref) < _MIN_PREFIX:
        raise GateError(422, "TWR-GEN-001", f"Identifier too short (min {_MIN_PREFIX} characters)")
    rows = await gate.list_level_overrides(acting, active=True, limit=500)
    matches = [r for r in rows if str(r["id"]).lower().startswith(ref)]
    if not matches:
        raise GateError(404, "TWR-FCK-032", "Level override not found")
    if len(matches) > 1:
        return ok(t(locale, "common.ambiguous", ref=ref))
    revoked = await gate.revoke_level_override(acting, str(matches[0]["id"]), reason=reason)
    return ok(t(locale, "level.revoke.done", short_id=short(revoked["id"])))
