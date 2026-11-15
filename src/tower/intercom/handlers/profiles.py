"""``/profile`` and ``/level`` cards (processing profiles, level overrides)."""

from __future__ import annotations

import json
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
    fmt_dt,
    guarded,
    listing,
    short,
    success,
)
from tower.intercom.i18n import t
from tower.intercom.ui import emojis

__all__ = [
    "CID_LEVEL_LIST",
    "CID_LEVEL_PICK",
    "CID_LEVEL_SET",
    "CID_PROFILE_HISTORY",
    "CID_PROFILE_ROLLBACK",
    "CID_PROFILE_SET",
    "CID_PROFILE_SHOW",
    "LEVELS",
    "TARGET_TYPES",
    "history",
    "level_list",
    "level_revoke",
    "level_set",
    "level_set_form",
    "parse_value",
    "rollback",
    "set_value",
    "show",
]

CID_PROFILE_SHOW = "t12a:prof:show"
CID_PROFILE_HISTORY = "t12a:prof:history"
CID_PROFILE_SET = "t12a:prof:set"
CID_PROFILE_ROLLBACK = "t12a:prof:rollback"
CID_LEVEL_LIST = "t12a:lvl:list"
CID_LEVEL_SET = "t12a:lvl:set"
CID_LEVEL_PICK = "t12a:lvl:pick"
LEVELS = ("N0", "N1", "N2", "N3", "N4")
TARGET_TYPES = ("content", "group", "account")
_MIN_PREFIX = 6
_SELECT_MAX = 25


def parse_value(raw: str) -> Any:
    """JSON value (``0.2``, ``true``, ``40``); anything else stays a string."""
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _yes_no(locale: str, value: object) -> str:
    return t(locale, "profile.yes" if value else "profile.no")


def _profile_buttons(locale: str, *, history: bool = True) -> tuple[Button, ...]:
    buttons = [
        Button(t(locale, "profile.btn.set"), CID_PROFILE_SET, emojis.EDIT, "primary"),
        Button(t(locale, "profile.btn.rollback"), CID_PROFILE_ROLLBACK, "", "danger"),
    ]
    if history:
        buttons.insert(
            0, Button(t(locale, "profile.btn.history"), CID_PROFILE_HISTORY, emojis.HISTORY)
        )
    return tuple(buttons)


@guarded
async def show(gate: GateClient, acting: int, locale: str) -> Reply:
    active = await gate.get_active_profile(acting)
    cfg = active["settings"]
    version = int(active["version"])
    items = tuple(
        t(
            locale,
            "profile.level.item",
            level=code(level),
            e1=code(s["e1_threshold"]),
            reuse=code(_yes_no(locale, s["direct_reuse"])),
            cap=code(s["unit_cap_usd"]),
            budget=code(s["monthly_budget_usd"]),
        )
        for level, s in cfg["levels"].items()
    )
    return card(
        t(locale, "profile.title"),
        t(
            locale,
            "profile.general",
            same=code(cfg["match_same_threshold"]),
            grey=code(cfg["match_grey_threshold"]),
            quota=code(cfg["draft_daily_quota"]),
            sample=code(cfg["e1_sample_rate"]),
        ),
        icon=emojis.SETTINGS,
        details=(
            (t(locale, "ops.d.version"), code(version)),
            (
                t(locale, "ops.d.origin"),
                code(t(locale, "profile.show.default" if version == 0 else "profile.show.stored")),
            ),
        ),
        items=items,
        buttons=_profile_buttons(locale),
    )


@guarded
async def history(gate: GateClient, acting: int, locale: str) -> Reply:
    rows = await gate.list_profiles(acting)
    lines = [
        t(
            locale,
            "profile.history.item",
            version=code(f"v{r['version']}"),
            active=t(locale, "profile.history.active") if r["active"] else "",
            author=code(r["author"]),
            created=fmt_dt(r.get("created_at")),
            reason=r["reason"],
        )
        for r in rows
    ]
    items, more = listing(locale, lines)
    return card(
        t(locale, "profile.history.title"),
        icon=emojis.HISTORY,
        breadcrumb=(t(locale, "profile.title"),),
        items=items,
        more=more,
        buttons=(Button(t(locale, "ops.back"), CID_PROFILE_SHOW, emojis.BACK),),
    )


def _back_to_profile(locale: str) -> tuple[Button, ...]:
    return (Button(t(locale, "profile.btn.back"), CID_PROFILE_SHOW, emojis.BACK),)


@guarded
async def set_value(
    gate: GateClient, acting: int, locale: str, path: str, value: str, reason: str
) -> Reply:
    parsed = parse_value(value.strip())
    created = await gate.create_profile(
        acting, changes={path.strip(): parsed}, reason=reason, activate=True
    )
    return success(
        t(locale, "profile.set.title"),
        t(locale, "profile.set.body", version=created["version"]),
        details=(
            (t(locale, "ops.d.path"), code(path.strip())),
            (t(locale, "ops.d.value"), code(json.dumps(parsed))),
        ),
        buttons=_back_to_profile(locale),
    )


@guarded
async def rollback(gate: GateClient, acting: int, locale: str, reason: str) -> Reply:
    profile = await gate.rollback_profile(acting, reason=reason)
    return success(
        t(locale, "profile.rollback.title"),
        t(locale, "profile.rollback.done", version=profile["version"]),
        buttons=_back_to_profile(locale),
    )


def _level_back(locale: str) -> tuple[Button, ...]:
    return (Button(t(locale, "level.btn.back"), CID_LEVEL_LIST, emojis.BACK),)


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
    return success(
        t(locale, "level.set.title"),
        t(locale, "level.set.applied" if created.get("applied") else "level.set.not_applied"),
        details=(
            (t(locale, "ops.d.level"), code(created["level"])),
            (t(locale, "ops.d.target"), code(f"{created['target_type']} {created['target']}")),
            (t(locale, "ops.d.expires"), code(fmt_dt(created["expires_at"]))),
            (t(locale, "ops.d.id"), code(short(created["id"]))),
        ),
        buttons=_level_back(locale),
    )


@guarded
async def level_set_form(
    gate: GateClient,
    acting: int,
    locale: str,
    target_type: str,
    target: str,
    level: str,
    hours_raw: str,
    reason: str,
) -> Reply:
    """``/level set`` modal: the duration arrives as typed text."""
    try:
        hours = float(hours_raw.strip().replace(",", "."))
    except ValueError:
        return error_card(
            t(locale, "level.bad_hours.title"),
            t(locale, "level.bad_hours.body"),
            severity="warning",
        )
    return await level_set(gate, acting, locale, target_type, target, level, hours, reason)


@guarded
async def level_list(gate: GateClient, acting: int, locale: str) -> Reply:
    rows = await gate.list_level_overrides(acting, active=True, limit=100)
    buttons = (
        Button(t(locale, "level.btn.set"), CID_LEVEL_SET, emojis.ADD, "primary"),
        Button(t(locale, "ops.refresh"), CID_LEVEL_LIST, emojis.SYNC),
    )
    if not rows:
        return card(
            t(locale, "level.title"),
            t(locale, "level.list.empty"),
            icon=emojis.FLAG,
            accent="neutral",
            buttons=buttons,
        )
    lines = [
        t(
            locale,
            "level.item",
            level=code(r["level"]),
            target_type=r["target_type"],
            target=code(r["target"]),
            expires=fmt_dt(r["expires_at"]),
            reason=r["reason"],
            short_id=code(short(r["id"])),
        )
        for r in rows
    ]
    items, more = listing(locale, lines)
    return card(
        t(locale, "level.title"),
        icon=emojis.FLAG,
        items=items,
        more=more,
        fields=(
            Field(
                t(locale, "level.revoke.label"),
                Select(
                    CID_LEVEL_PICK,
                    placeholder=t(locale, "level.revoke.placeholder"),
                    options=tuple(
                        Option(
                            str(r["id"]),
                            f"{r['level']} · {r['target_type']}",
                            description=str(r["target"])[:100],
                        )
                        for r in rows[:_SELECT_MAX]
                    ),
                ),
                help=t(locale, "level.revoke.help"),
            ),
        ),
        buttons=buttons,
    )


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
        return error_card(
            t(locale, "common.ambiguous.title"),
            t(locale, "common.ambiguous", ref=ref),
            severity="warning",
        )
    revoked = await gate.revoke_level_override(acting, str(matches[0]["id"]), reason=reason)
    return success(
        t(locale, "level.revoke.title"),
        t(locale, "level.revoke.done", short_id=short(revoked["id"])),
        buttons=_level_back(locale),
    )
