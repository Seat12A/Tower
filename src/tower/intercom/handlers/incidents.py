"""``/incidents`` cards (stored errors): list with show and resolve selects, detail card
(message, context, catalog hint, traceback excerpt), resolve with a note."""

from __future__ import annotations

import json
import re
from typing import Any

from tower.intercom.gate_client import GateClient
from tower.intercom.handlers import (
    MAX_MESSAGE,
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
    "CID_LIST",
    "CID_PICK",
    "CID_RESOLVE_PREFIX",
    "CID_SHOW",
    "list_incidents",
    "resolve",
    "show",
]

CID_LIST = "t12a:inc:list"
CID_PICK = "t12a:inc:pick"
CID_SHOW = "t12a:inc:show"
CID_RESOLVE_PREFIX = "t12a:inc:res:"
"""Followed by the fingerprint (lowercase hex): opens the resolve form of that incident."""
_MIN_PREFIX = 6
_SELECT_MAX = 25
_FINGERPRINT = re.compile(r"[0-9a-f]{6,64}")
_CODE = re.compile(r"TWR-[A-Z]+-\d{3}")
_MAX_CONTEXT_ITEMS = 12
_MAX_CONTEXT_VALUE = 120
_MAX_CONTEXT = 1200
_MAX_MESSAGE = 600
_MAX_TRACEBACK = 1500
_MIN_TRACEBACK = 200
_BODY_BUDGET = MAX_MESSAGE - 800
"""Body room left once the title, the details and the footer are counted."""
_ACCENTS: dict[str, Any] = {
    "info": "neutral",
    "warning": "warning",
    "error": "error",
    "critical": "critical",
}


@guarded
async def list_incidents(
    gate: GateClient,
    acting: int,
    locale: str,
    limit: int = 10,
    *,
    code_filter: str | None = None,
    include_resolved: bool = False,
) -> Reply:
    wanted = (code_filter or "").strip().upper() or None
    if wanted is not None and not _CODE.fullmatch(wanted):
        return error_card(
            t(locale, "incidents.code.bad.title"),
            t(locale, "incidents.code.bad", code=code(wanted)),
            severity="warning",
        )
    rows = await gate.list_incidents(
        acting, limit=limit, code=wanted, include_resolved=include_resolved
    )
    refresh = Button(t(locale, "ops.refresh"), CID_LIST, emojis.SYNC)
    title = t(locale, "incidents.title.all" if include_resolved else "incidents.title")
    if wanted is not None:
        title = f"{title} · {wanted}"
    if not rows:
        empty = (
            "incidents.list.empty"
            if wanted is None and not include_resolved
            else ("incidents.list.empty.filtered")
        )
        return card(
            title,
            t(locale, empty),
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
    shown = rows[:_SELECT_MAX]
    open_rows = [r for r in shown if not r.get("resolved_at")]
    fields = [
        Field(
            t(locale, "incidents.show.label"),
            Select(
                CID_SHOW,
                placeholder=t(locale, "incidents.show.placeholder"),
                options=tuple(_option(r) for r in shown),
            ),
            help=t(locale, "incidents.show.help"),
        )
    ]
    if open_rows:
        fields.append(
            Field(
                t(locale, "incidents.resolve.label"),
                Select(
                    CID_PICK,
                    placeholder=t(locale, "incidents.resolve.placeholder"),
                    options=tuple(_option(r) for r in open_rows),
                ),
                help=t(locale, "incidents.resolve.help"),
            )
        )
    return card(
        title,
        icon=emojis.WARNING,
        items=items,
        more=more,
        fields=tuple(fields),
        buttons=(refresh,),
    )


def _option(row: dict[str, Any]) -> Option:
    return Option(
        str(row["fingerprint"])[:100],
        f"{row['code']} · {short(row['fingerprint'], 12)}"[:100],
        description=str(row.get("title") or "")[:100],
    )


async def _target(
    gate: GateClient, acting: int, locale: str, ref: str, *, include_resolved: bool
) -> str | Reply:
    """Full fingerprint for a prefix of at least 6 characters (or the ref as is)."""
    ref = ref.strip().lower()
    if len(ref) < _MIN_PREFIX:
        return ref
    rows = await gate.list_incidents(acting, limit=200, include_resolved=include_resolved)
    matches = [str(r["fingerprint"]) for r in rows if str(r["fingerprint"]).startswith(ref)]
    if len(matches) > 1:
        return error_card(
            t(locale, "common.ambiguous.title"),
            t(locale, "common.ambiguous", ref=ref),
            severity="warning",
        )
    return matches[0] if matches else ref


def _block(text: str) -> str:
    """Text in a code block, neutralising the fences it may contain."""
    return "```\n" + text.replace("```", "ˋˋˋ") + "\n```"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tail(text: str, limit: int) -> str:
    """End of a traceback (the failing frame and the exception are at the bottom)."""
    return text if len(text) <= limit else "…" + text[-(limit - 1) :]


def _context_value(value: object) -> str:
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return _clip(raw, _MAX_CONTEXT_VALUE)


@guarded
async def show(gate: GateClient, acting: int, locale: str, fingerprint: str) -> Reply:
    target = await _target(gate, acting, locale, fingerprint, include_resolved=True)
    if isinstance(target, Reply):
        return target
    r = await gate.get_incident(acting, target)
    fp = str(r["fingerprint"])
    yes_no = t(locale, "incidents.detail.yes" if r.get("retryable") else "incidents.detail.no")
    if r.get("resolved_at"):
        status = t(
            locale,
            "incidents.detail.resolved",
            when=fmt_dt(r.get("resolved_at")),
            who=r.get("resolved_by") or "-",
        )
    else:
        status = t(locale, "incidents.detail.open")
    details: list[tuple[str, str]] = [
        (t(locale, "incidents.detail.status"), status),
        (t(locale, "incidents.detail.severity"), code(r.get("severity") or "-")),
        (t(locale, "incidents.detail.occurrences"), str(r.get("occurrences", 1))),
        (t(locale, "incidents.detail.first_seen"), fmt_dt(r.get("first_seen"))),
        (t(locale, "incidents.detail.last_seen"), fmt_dt(r.get("last_seen"))),
        (t(locale, "incidents.detail.process"), code(r.get("process") or "-")),
        (t(locale, "incidents.detail.location"), code(r.get("location") or "-")),
        (t(locale, "incidents.detail.exception"), code(r.get("exception_type") or "-")),
        (t(locale, "incidents.detail.retryable"), yes_no),
    ]
    if r.get("resolution_note"):
        details.append(
            (t(locale, "incidents.detail.note"), code(_clip(str(r["resolution_note"]), 300)))
        )
    parts: list[str] = []
    if r.get("last_message"):
        parts.append(
            f"**{t(locale, 'incidents.detail.message')}**\n"
            + _block(_clip(str(r["last_message"]), _MAX_MESSAGE))
        )
    if r.get("hint"):
        parts.append(f"**{t(locale, 'incidents.detail.hint')}**\n{r['hint']}")
    context = r.get("last_context") or {}
    if isinstance(context, dict) and context:
        lines = [
            f"{k} = {_context_value(v)}" for k, v in list(context.items())[:_MAX_CONTEXT_ITEMS]
        ]
        text = _clip("\n".join(lines), _MAX_CONTEXT)
        if len(context) > _MAX_CONTEXT_ITEMS:
            text += "\n" + t(locale, "common.more", count=len(context) - _MAX_CONTEXT_ITEMS)
        parts.append(f"**{t(locale, 'incidents.detail.context')}**\n" + _block(text))
    room = _BODY_BUDGET - sum(len(p) + 2 for p in parts) - 60
    if r.get("last_traceback") and room >= _MIN_TRACEBACK:
        parts.append(
            f"**{t(locale, 'incidents.detail.traceback')}**\n"
            + _block(_tail(str(r["last_traceback"]), min(room, _MAX_TRACEBACK)))
        )
    buttons = [Button(t(locale, "incidents.btn.back"), CID_LIST, emojis.BACK)]
    if not r.get("resolved_at") and _FINGERPRINT.fullmatch(fp):
        buttons.insert(0, Button(t(locale, "incidents.btn.resolve"), CID_RESOLVE_PREFIX + fp))
    return card(
        f"{r.get('code', '')} · {r.get('title') or ''}",
        "\n\n".join(parts),
        icon=emojis.WARNING,
        accent=_ACCENTS.get(str(r.get("severity")), "neutral"),
        details=tuple(details),
        footer=code(fp),
        buttons=tuple(buttons),
    )


@guarded
async def resolve(
    gate: GateClient, acting: int, locale: str, fingerprint: str, note: str | None = None
) -> Reply:
    target = await _target(gate, acting, locale, fingerprint, include_resolved=False)
    if isinstance(target, Reply):
        return target
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
