"""``/narrative`` cards and the wave notifications of the bot (gate client only)."""

from __future__ import annotations

import re
from typing import Any

from tower.intercom.gate_client import GateClient, GateError
from tower.intercom.handlers import (
    Accent,
    Button,
    Field,
    Option,
    Reply,
    Select,
    card,
    code,
    fmt_usd,
    guarded,
    listing,
    short,
    success,
)
from tower.intercom.i18n import has, t
from tower.intercom.ui import emojis

__all__ = [
    "NARRATIVE_ACTIONS",
    "format_surge_detected",
    "format_surge_ended",
    "narrative_custom_id",
    "narrative_list",
    "narrative_merge",
    "narrative_show",
    "narrative_split",
    "narrative_title",
]

NARRATIVE_ACTIONS = ("open", "show", "titleok", "title", "merge", "split")
"""Button actions of a narrative card: ``open`` sends the card as a new private message
(public notifications), ``show`` refreshes it in place (private panels)."""
PICK_ACTION = "pick"
"""Action of the select of ``/narrative list``."""

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_TITLE_MAX = 80
_MAX_SPLIT_GROUPS = 200


def _uuid_arg(value: str) -> str:
    ref = value.strip()
    if not _UUID.fullmatch(ref):
        raise GateError(422, "TWR-GEN-001", "A full identifier is required")
    return ref.lower()


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _state(locale: str, state: object) -> str:
    key = f"narrative.state.{state}"
    return t(locale, key) if has(locale, key) or has("en", key) else str(state)


def _title(locale: str, title: object) -> str:
    return _clip(str(title), _TITLE_MAX) if title else t(locale, "narrative.untitled")


def _pct(share: object) -> str:
    try:
        return f"{float(str(share)) * 100:.1f}"
    except ValueError:
        return "?"


def _day(value: object) -> str:
    return str(value)[:10] if value else "-"


def _duration(seconds: object) -> str:
    try:
        total = int(str(seconds))
    except ValueError:
        return "?"
    hours, rest = divmod(total, 3600)
    return f"{hours}h{rest // 60:02d}" if hours else f"{rest // 60} min"


def narrative_custom_id(action: str, narrative_id: str = "") -> str:
    return f"t12a:nar:{action}:{narrative_id}" if narrative_id else f"t12a:nar:{action}"


def _footer_lines(lines: list[str]) -> str:
    """Several grey lines: the renderer prefixes only the first one with ``-#``."""
    return "\n-# ".join(lines)


def _show_button(locale: str, narrative_id: object) -> Button:
    return Button(
        t(locale, "narrative.button.show"),
        narrative_custom_id("show", str(narrative_id)),
        icon=emojis.TRACK,
    )


# --- notifications ----------------------------------------------------------------------


def _platforms(value: object) -> str:
    items = value if isinstance(value, list) else []
    return ", ".join(str(p) for p in items) or "-"


def format_surge_detected(payload: dict[str, Any], locale: str) -> Reply:
    """Card of ``narrative.surge_detected`` (tier ``surge`` = warning, ``alert`` = error)."""
    nid = str(payload.get("narrative_id", "?"))
    alert = payload.get("tier") == "alert"
    return Reply(
        title=t(
            locale, "notify.narrative.alert.title" if alert else "notify.narrative.surge.title"
        ),
        icon=emojis.NOTIFICATIONS,
        accent="error" if alert else "warning",
        body=_title(locale, payload.get("title")),
        details=(
            (t(locale, "narrative.detail.id"), code(short(nid))),
            (t(locale, "narrative.detail.share"), code(f"{_pct(payload.get('share', 0))}%")),
            (t(locale, "narrative.detail.accounts"), code(payload.get("accounts", "?"))),
            (t(locale, "narrative.detail.platforms"), code(_platforms(payload.get("platforms")))),
        ),
        footer=t(locale, "notify.narrative.alert.footer") if alert else "",
        buttons=(
            Button(
                t(locale, "narrative.button.open"),
                narrative_custom_id("open", nid),
                icon=emojis.TRACK,
            ),
        )
        if _UUID.fullmatch(nid)
        else (),
        ephemeral=False,
    )


def format_surge_ended(payload: dict[str, Any], locale: str) -> Reply:
    """Card of ``narrative.surge_ended`` (the end-of-wave report)."""
    nid = str(payload.get("narrative_id", "?"))
    delay = payload.get("first_draft_delay_s")
    return Reply(
        title=t(locale, "notify.narrative.ended.title"),
        icon=emojis.HISTORY,
        accent="primary",
        body=_title(locale, payload.get("title")) if payload.get("title") else "",
        details=(
            (t(locale, "narrative.detail.id"), code(short(nid))),
            (t(locale, "narrative.detail.duration"), code(_duration(payload.get("duration_s", 0)))),
            (t(locale, "narrative.detail.posts"), code(payload.get("posts", "?"))),
            (t(locale, "narrative.detail.platforms"), code(_platforms(payload.get("platforms")))),
            (
                t(locale, "narrative.detail.cost"),
                code(f"${fmt_usd(payload.get('cost_usd_micros', 0))}"),
            ),
            (
                t(locale, "narrative.detail.first_draft"),
                code(_duration(delay) if delay is not None else "-"),
            ),
        ),
        buttons=(
            Button(
                t(locale, "narrative.button.open"),
                narrative_custom_id("open", nid),
                icon=emojis.TRACK,
            ),
        )
        if _UUID.fullmatch(nid)
        else (),
        ephemeral=False,
    )


# --- commands ---------------------------------------------------------------------------

_STATE_ACCENT: dict[str, Accent] = {
    "surge": "warning",
    "alert": "error",
    "closed": "neutral",
}


@guarded
async def narrative_list(
    gate: GateClient, acting: int, locale: str, state: str | None = None
) -> Reply:
    rows = await gate.list_narratives(acting, state=state or None)
    title = t(locale, "narrative.list.title")
    if not rows:
        return card(title, t(locale, "narrative.list.empty"), icon=emojis.TRACK, accent="neutral")
    lines = []
    for row in rows:
        stats = row.get("stats_1h") or {}
        lines.append(
            t(
                locale,
                "narrative.list.line",
                short_id=short(row["id"]),
                state=_state(locale, row["state"]),
                title=_title(locale, row.get("title")),
                groups=row.get("groups", 0),
                share=_pct(stats.get("share", 0)),
                accounts=stats.get("accounts", 0),
            )
        )
    items, more = listing(locale, lines)
    picker = Select(
        narrative_custom_id(PICK_ACTION),
        placeholder=t(locale, "narrative.list.open"),
        options=tuple(
            Option(
                value=str(row["id"]),
                label=_title(locale, row.get("title")),
                description=_state(locale, row["state"]),
            )
            for row in rows[: len(items)][:25]
        ),
    )
    return Reply(
        title=title,
        icon=emojis.TRACK,
        items=items,
        more=more,
        fields=(Field(t(locale, "narrative.list.open.label"), picker),),
    )


@guarded
async def narrative_show(gate: GateClient, acting: int, locale: str, narrative_id: str) -> Reply:
    n = await gate.get_narrative(acting, _uuid_arg(narrative_id))
    nid = str(n["id"])
    state = str(n["state"])
    title_status = str(n.get("title_status"))
    details: list[tuple[str, str]] = [
        (t(locale, "narrative.detail.id"), code(nid)),
        (t(locale, "narrative.detail.state"), code(_state(locale, state))),
        (
            t(locale, "narrative.detail.title_status"),
            code(t(locale, f"narrative.title_status.{title_status}")),
        ),
    ]
    stats = n.get("stats_1h")
    if stats:
        details.append(
            (
                t(locale, "narrative.detail.last_hour"),
                t(
                    locale,
                    "narrative.show.stats",
                    share=code(f"{_pct(stats['share'])}%"),
                    accounts=code(stats["accounts"]),
                    platforms=code(stats["platforms"]),
                ),
            )
        )
    if n.get("representative_group"):
        details.append(
            (t(locale, "narrative.detail.representative"), code(n["representative_group"]))
        )
    if n.get("previous_narrative"):
        details.append((t(locale, "narrative.detail.previous"), code(n["previous_narrative"])))
    lines = []
    for g in (n.get("groups") or [])[:8]:
        verdict = (g.get("verdict") or {}).get("veracity") or "-"
        lines.append(
            t(
                locale,
                "narrative.show.group",
                group_id=g["id"],
                level=g["level"],
                verdict=verdict,
                signal=g.get("light_signal") or "-",
                reach=g.get("reach", 0),
            )
        )
    for item in (n.get("novelty") or [])[:3]:
        lines.append(
            t(
                locale,
                "narrative.show.novelty",
                kind=item["kind"],
                fingerprint=_clip(item["fingerprint"], 60),
                posts=item["posts"],
                accounts=item["accounts"],
            )
        )
    items, more = listing(locale, lines)
    journal = [
        t(
            locale,
            "narrative.show.event",
            kind=e["kind"],
            actor=e["actor"],
            when=_day(e.get("created_at")),
        )
        for e in (n.get("journal") or [])[:3]
    ]
    body = _title(locale, n.get("title"))
    if n.get("split_suggested"):
        body += "\n" + t(locale, "narrative.show.split_suggested")
    return Reply(
        title=t(locale, "narrative.show.title"),
        icon=emojis.TRACK,
        accent=_STATE_ACCENT.get(state, "primary"),
        body=body,
        details=tuple(details),
        items=items,
        more=more,
        footer=_footer_lines(journal),
        buttons=(
            Button(
                t(locale, "narrative.button.refresh"),
                narrative_custom_id("show", nid),
                icon=emojis.SYNC,
            ),
            Button(
                t(locale, "narrative.button.titleok"),
                narrative_custom_id("titleok", nid),
                icon=emojis.DONE,
                style="success",
                disabled=not (title_status == "draft" and n.get("title")),
            ),
            Button(
                t(locale, "narrative.button.title"),
                narrative_custom_id("title", nid),
                icon=emojis.EDIT,
            ),
            Button(
                t(locale, "narrative.button.merge"),
                narrative_custom_id("merge", nid),
                icon=emojis.GROUPS,
            ),
            Button(t(locale, "narrative.button.split"), narrative_custom_id("split", nid)),
        ),
    )


@guarded
async def narrative_merge(
    gate: GateClient, acting: int, locale: str, target: str, source: str, reason: str
) -> Reply:
    merged = await gate.merge_narratives(
        acting, _uuid_arg(target), source_id=_uuid_arg(source), reason=reason.strip()
    )
    groups = merged.get("groups_count", len(merged.get("groups", [])))
    return success(
        t(locale, "narrative.merged.title"),
        t(locale, "narrative.merged"),
        details=(
            (t(locale, "narrative.detail.id"), code(merged["id"])),
            (t(locale, "narrative.detail.groups"), code(groups)),
        ),
        buttons=(_show_button(locale, merged["id"]),),
    )


@guarded
async def narrative_split(
    gate: GateClient, acting: int, locale: str, narrative_id: str, group_ids: str, reason: str
) -> Reply:
    ids = [_uuid_arg(part) for part in re.split(r"[,\s]+", group_ids.strip()) if part]
    if not ids or len(ids) > _MAX_SPLIT_GROUPS:
        raise GateError(422, "TWR-GEN-001", "Give the group ids to move, comma separated")
    new = await gate.split_narrative(
        acting, _uuid_arg(narrative_id), group_ids=ids, reason=reason.strip()
    )
    return success(
        t(locale, "narrative.split.title"),
        t(locale, "narrative.split"),
        details=((t(locale, "narrative.detail.id"), code(new["id"])),),
        buttons=(_show_button(locale, new["id"]),),
    )


@guarded
async def narrative_title(
    gate: GateClient, acting: int, locale: str, narrative_id: str, title: str | None = None
) -> Reply:
    done = await gate.set_narrative_title(
        acting, _uuid_arg(narrative_id), title=(title or "").strip() or None
    )
    return success(
        t(locale, "narrative.title.title"),
        _title(locale, done["title"]),
        details=((t(locale, "narrative.detail.id"), code(done["id"])),),
        buttons=(_show_button(locale, done["id"]),),
    )
