"""``/narrative`` commands and the wave notifications of the bot (gate client only)."""

from __future__ import annotations

import re
from typing import Any

from tower.intercom.gate_client import GateClient, GateError
from tower.intercom.handlers import Reply, fmt_usd, guarded, listing, ok, short
from tower.intercom.i18n import has, t

__all__ = [
    "format_surge_detected",
    "format_surge_ended",
    "narrative_list",
    "narrative_merge",
    "narrative_show",
    "narrative_split",
    "narrative_title",
]

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


# --- notifications ----------------------------------------------------------------------


def _platforms(value: object) -> str:
    items = value if isinstance(value, list) else []
    return ", ".join(str(p) for p in items) or "-"


def format_surge_detected(payload: dict[str, Any], locale: str) -> str:
    """Text of ``narrative.surge_detected`` (tier ``surge`` or ``alert``)."""
    nid = str(payload.get("narrative_id", "?"))
    key = "notify.narrative.alert" if payload.get("tier") == "alert" else "notify.narrative.surge"
    return t(
        locale,
        key,
        narrative_id=nid,
        short_id=short(nid),
        title=_title(locale, payload.get("title")),
        share=_pct(payload.get("share", 0)),
        accounts=payload.get("accounts", "?"),
        platforms=_platforms(payload.get("platforms")),
    )


def format_surge_ended(payload: dict[str, Any], locale: str) -> str:
    """Text of ``narrative.surge_ended`` (the end-of-wave report)."""
    nid = str(payload.get("narrative_id", "?"))
    delay = payload.get("first_draft_delay_s")
    return t(
        locale,
        "notify.narrative.ended",
        short_id=short(nid),
        duration=_duration(payload.get("duration_s", 0)),
        posts=payload.get("posts", "?"),
        platforms=_platforms(payload.get("platforms")),
        cost=fmt_usd(payload.get("cost_usd_micros", 0)),
        first_draft=_duration(delay) if delay is not None else "-",
    )


# --- commands ---------------------------------------------------------------------------


@guarded
async def narrative_list(
    gate: GateClient, acting: int, locale: str, state: str | None = None
) -> Reply:
    rows = await gate.list_narratives(acting, state=state or None)
    if not rows:
        return ok(t(locale, "narrative.list.empty"))
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
    return ok(listing(locale, t(locale, "narrative.list.header", count=len(rows)), lines))


@guarded
async def narrative_show(gate: GateClient, acting: int, locale: str, narrative_id: str) -> Reply:
    n = await gate.get_narrative(acting, _uuid_arg(narrative_id))
    title_status = f"narrative.title_status.{n.get('title_status')}"
    lines = [
        t(
            locale,
            "narrative.show.header",
            narrative_id=n["id"],
            state=_state(locale, n["state"]),
            title=_title(locale, n.get("title")),
            title_status=t(locale, title_status),
        )
    ]
    stats = n.get("stats_1h")
    if stats:
        lines.append(
            t(
                locale,
                "narrative.show.stats",
                share=_pct(stats["share"]),
                accounts=stats["accounts"],
                platforms=stats["platforms"],
            )
        )
    if n.get("split_suggested"):
        lines.append(t(locale, "narrative.show.split_suggested"))
    if n.get("previous_narrative"):
        lines.append(t(locale, "narrative.show.previous", narrative_id=n["previous_narrative"]))
    if n.get("representative_group"):
        lines.append(t(locale, "narrative.show.representative", group_id=n["representative_group"]))
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
    for e in (n.get("journal") or [])[:5]:
        lines.append(
            t(
                locale,
                "narrative.show.event",
                kind=e["kind"],
                actor=e["actor"],
                when=_day(e.get("created_at")),
            )
        )
    return ok(listing(locale, lines[0], lines[1:]))


@guarded
async def narrative_merge(
    gate: GateClient, acting: int, locale: str, target: str, source: str, reason: str
) -> Reply:
    merged = await gate.merge_narratives(
        acting, _uuid_arg(target), source_id=_uuid_arg(source), reason=reason.strip()
    )
    return ok(
        t(
            locale,
            "narrative.merged",
            narrative_id=merged["id"],
            groups=merged.get("groups_count", len(merged.get("groups", []))),
        )
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
    return ok(t(locale, "narrative.split", narrative_id=new["id"]))


@guarded
async def narrative_title(
    gate: GateClient, acting: int, locale: str, narrative_id: str, title: str | None = None
) -> Reply:
    done = await gate.set_narrative_title(
        acting, _uuid_arg(narrative_id), title=(title or "").strip() or None
    )
    return ok(
        t(
            locale,
            "narrative.title.done",
            narrative_id=done["id"],
            title=_title(locale, done["title"]),
        )
    )
