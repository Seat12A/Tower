"""Fact-check in Discord: draft rendering (pure), review actions, ``/factcheck`` and the
startup reconciliation. Independent of discord.py: the app wires buttons and modals to
the functions below.

``render_draft`` returns plain-text message parts (Discord markdown) of at most
``MAX_MESSAGE`` characters each; links are wrapped in ``<...>`` (no embed preview) and a
part is never cut inside a URL.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Awaitable, Callable
from typing import Any

from tower.core.domain import error_codes as ec
from tower.core.errors import capture
from tower.core.logging import get_logger
from tower.intercom.gate_client import GateClient, GateError
from tower.intercom.handlers import MAX_MESSAGE, Reply, guarded, listing, ok, short
from tower.intercom.i18n import has, t

__all__ = [
    "ACTIONS",
    "VERACITIES",
    "band_validate",
    "custom_id",
    "fact_create",
    "fact_list",
    "fact_reject",
    "fact_show",
    "fact_validate",
    "group_merge",
    "group_merge_same",
    "group_show",
    "group_split",
    "group_split_draft",
    "parse_custom_id",
    "parse_veracity",
    "post_draft",
    "reconcile_drafts",
    "render_draft",
    "request",
    "review_accept",
    "review_edit",
    "review_reject",
    "split_text",
    "veracity_label",
]

log = get_logger(__name__)

ACTIONS = ("validate", "correct", "reject", "samegroup", "split", "fact", "band")
VERACITIES = ("true", "mostly_true", "misleading", "false", "unverifiable")
_CUSTOM_ID = re.compile(
    r"^fc:(validate|correct|reject|samegroup|split|fact|band):([0-9a-fA-F-]{36})$"
)
_QUOTE_MAX = 300
_ASSERTION_MAX = 400
_MAX_PARTS = 10

SendDraft = Callable[[str, list[str], str], Awaitable[bool]]
"""``(channel key, message parts, draft id) -> posted``: posts the parts, the buttons go
on the last one; ``False`` when nothing was posted (channel not configured)."""


# --- custom ids ----------------------------------------------------------------------


def custom_id(action: str, draft_id: str) -> str:
    """Persistent component id (survives restarts): ``fc:<action>:<draft id>``."""
    return f"fc:{action}:{draft_id}"


def parse_custom_id(value: str) -> tuple[str, str] | None:
    match = _CUSTOM_ID.match(value)
    return (match.group(1), match.group(2)) if match else None


# --- vocabulary ----------------------------------------------------------------------


def veracity_label(locale: str, value: str) -> str:
    key = f"veracity.{value}"
    return t(locale, key) if has(locale, key) else value


def _fold(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[\s_-]+", " ", stripped).strip()


def parse_veracity(locale: str, value: str) -> str | None:
    """Veracity value of what the member typed: a value (``mostly_true``) or a label in
    either language (``Plutôt vrai``, ``mostly true``), accents and case ignored."""
    wanted = _fold(value)
    if not wanted:
        return None
    for veracity in VERACITIES:
        candidates = {veracity, t("fr", f"veracity.{veracity}"), t("en", f"veracity.{veracity}")}
        candidates.add(t(locale, f"veracity.{veracity}"))
        if wanted in {_fold(c) for c in candidates}:
            return veracity
    return None


def _label(locale: str, prefix: str, value: object) -> str:
    key = f"{prefix}.{value}"
    return t(locale, key) if has(locale, key) else str(value)


# --- text helpers --------------------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    head, _, _tail = cut.rpartition(" ")
    return (head or cut).rstrip() + "…"


def _quote_block(text: str) -> str:
    return "\n".join("> " + line if line else ">" for line in text.splitlines())


def _cut(rest: str, limit: int) -> tuple[int, int]:
    """``(end, skip)``: ``rest[:end]`` is the piece, ``skip`` separator chars follow."""
    window = rest[: limit + 1]
    newline = window.rfind("\n")
    if newline > 0:
        return newline, 1
    space = window.rfind(" ")
    if space > 0:
        return space, 1
    return limit, 0  # a single token longer than the limit: nothing better exists


def split_text(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """Cut ``text`` into pieces of at most ``limit`` characters at line breaks, else at
    spaces (so a URL, which has none, is never cut unless it alone exceeds ``limit``)."""
    pieces: list[str] = []
    rest = text
    while len(rest) > limit:
        end, skip = _cut(rest, limit)
        pieces.append(rest[:end].rstrip())
        rest = rest[end + skip :].lstrip("\n")
    if rest.strip():
        pieces.append(rest)
    return pieces


def _pack(blocks: list[str], limit: int) -> list[str]:
    parts: list[str] = []
    current = ""
    for block in blocks:
        for piece in split_text(block, limit):
            if current and len(current) + 2 + len(piece) <= limit:
                current = f"{current}\n\n{piece}"
            else:
                if current:
                    parts.append(current)
                current = piece
    if current:
        parts.append(current)
    return parts


# --- draft rendering -----------------------------------------------------------------


def _header(view: dict[str, Any], locale: str) -> str:
    confidence = view.get("confidence")
    pct = f"{round(float(confidence) * 100)}%" if confidence is not None else "?"
    band = view.get("confidence_band")
    depth = t(locale, "draft.depth.reinforced") if view.get("depth") == "reinforced" else ""
    return (
        t(locale, "draft.title")
        + "\n"
        + t(
            locale,
            "draft.header",
            veracity=veracity_label(locale, str(view.get("veracity"))),
            context_need=_label(locale, "context_need", view.get("context_need")),
            confidence=pct,
            band=_label(locale, "band", band) if band else "-",
            level=view.get("level", "?"),
            depth=depth,
        )
    )


def _claim_block(view: dict[str, Any], locale: str) -> str:
    claim = view.get("claim") or {}
    text_fr = claim.get("text_fr") or claim.get("text_original") or ""
    if locale == "en" and claim.get("lang") == "en" and claim.get("text_original"):
        text, tag = claim["text_original"], ""
    elif locale == "en":
        text, tag = text_fr, t(locale, "draft.claim.fr_tag")
    else:
        text, tag = text_fr, ""
    lines = [t(locale, "draft.claim.header", tag=tag), text]
    url = (view.get("content") or {}).get("url")
    if url:
        lines.append(t(locale, "draft.post_link", url=url))
    return "\n".join(lines)


def _links(posts: list[dict[str, Any]], limit: int = 5) -> str:
    shown = [f"{p['platform']} <{p['url']}>" for p in posts if p.get("url")][:limit]
    extra = len(posts) - len(shown)
    return ", ".join(shown) + (f" (+{extra})" if extra > 0 else "")


def _group_block(view: dict[str, Any], locale: str) -> str | None:
    group = view.get("group")
    if not group:
        return None
    lines = [
        t(
            locale,
            "draft.group.line",
            group_id=group["id"],
            members=group["members"],
            platforms=", ".join(view.get("platforms") or []) or "-",
        )
    ]
    posts = view.get("other_posts") or []
    if posts:
        lines.append(t(locale, "draft.group.other_posts", links=_links(posts)))
    related = view.get("reusable_verdict")
    if related:
        lines.append(
            t(
                locale,
                "draft.group.related",
                group_id=related["group_id"],
                veracity=veracity_label(locale, str(related["veracity"])),
            )
        )
    return "\n".join(lines)


def _narrative_block(view: dict[str, Any], locale: str) -> str | None:
    narrative = view.get("narrative")
    if not narrative:
        return None
    key = f"narrative.state.{narrative.get('state')}"
    return t(
        locale,
        "draft.narrative.line",
        narrative_id=narrative["id"],
        title=_clip(narrative["title"], 100)
        if narrative.get("title")
        else t(locale, "narrative.untitled"),
        state=t(locale, key) if has(locale, key) else narrative.get("state", "?"),
        groups=narrative.get("groups", 0),
    )


def _coordination_block(view: dict[str, Any], locale: str) -> str | None:
    signals = view.get("coordination") or []
    if not signals:
        return None
    lines = [t(locale, "draft.coordination.header")]
    for signal in signals:
        kind = str(signal.get("kind", "?"))
        key = f"draft.coordination.{kind}"
        detail = signal.get("detail") or {}
        if not has(locale, key):
            key, extra = "draft.coordination.other", {"kind": kind}
        else:
            extra = {}
        lines.append(
            t(
                locale,
                key,
                count=signal.get("count", 0),
                minutes=signal.get("window_minutes", 0),
                authors=detail.get("authors", "?"),
                **extra,
            )
        )
    return "\n".join(lines)


def _fact_block(view: dict[str, Any], locale: str) -> str | None:
    fact = view.get("reference_fact")
    lines: list[str] = []
    if fact:
        lines.append(
            t(
                locale,
                "draft.fact.line",
                label=fact["label"],
                value=fact["value"],
                unit=fact["unit"],
                start=fact["period_start"],
                end=fact["period_end"],
                territory=fact["territory"],
                status=_label(locale, "fact.status", fact["status"]),
            )
        )
    band = view.get("band")
    if band:
        side = f" ({band['side']})" if band.get("side") not in (None, "none") else ""
        label = _label(locale, "band.kind", band["band"]) + side
        lines.append(t(locale, "draft.fact.band", label=label))
    if view.get("outdated"):
        lines.append(t(locale, "draft.fact.outdated"))
    return "\n".join(lines) if lines else None


def _external_block(view: dict[str, Any], locale: str) -> str | None:
    items = view.get("external_factchecks") or []
    if not items:
        return None
    lines = [t(locale, "draft.external.header")]
    for item in items[:3]:
        lines.append(
            t(
                locale,
                "draft.external.line",
                publisher=_clip(item.get("publisher") or "?", 60),
                rating=_clip(item.get("rating") or "?", 80),
                url=item.get("url", "?"),
            )
        )
    return "\n".join(lines)


def _note_block(view: dict[str, Any], locale: str) -> str:
    notes: dict[str, dict[str, Any]] = view.get("notes") or {}
    order = ["en", "fr"] if locale == "en" else ["fr"]
    lang = next((c for c in order if c in notes), next(iter(notes), None))
    if lang is None:
        return t(locale, "draft.note.none")
    text = str(notes[lang].get("text", ""))
    return t(locale, "draft.note.header", lang=lang) + "\n" + _quote_block(text)


def _rationale_block(view: dict[str, Any], locale: str) -> str | None:
    lines: list[str] = []
    for item in view.get("rationale") or []:
        lines.append(
            t(
                locale,
                "draft.rationale.assertion",
                assertion=_clip(item["assertion"], _ASSERTION_MAX),
            )
        )
        for cite in item.get("citations") or []:
            if not cite.get("valid", True):
                continue
            src = cite.get("source") or {}
            name = src.get("publisher") or src.get("title") or "?"
            source = t(locale, "draft.rationale.source", name=name, url=src.get("url", "?"))
            lines.append(
                t(
                    locale,
                    "draft.rationale.citation",
                    label=cite.get("label") or "?",
                    quote=_clip(cite.get("quote", ""), _QUOTE_MAX),
                    source=source,
                )
            )
            translation = cite.get("translation_fr")
            if locale == "fr" and translation and cite.get("lang") not in (None, "fr"):
                lines.append(
                    t(locale, "draft.rationale.translation", text=_clip(translation, _QUOTE_MAX))
                )
    if not lines:
        return None
    return "\n".join([t(locale, "draft.rationale.header"), *lines])


def _list_block(header: str, items: list[str]) -> str | None:
    clean = [_clip(i, _ASSERTION_MAX) for i in items if str(i).strip()]
    if not clean:
        return None
    return "\n".join([header, *[f"- {i}" for i in clean]])


def _review_points_block(view: dict[str, Any], locale: str) -> str | None:
    objection = view.get("objection")
    lines: list[str] = []
    skip: str | None = None
    if objection:
        skip = str(objection.get("explanation", "")).strip()
        lines.append(
            t(
                locale,
                "draft.objection",
                type=_label(locale, "objection.type", objection.get("type", "?")),
                direction=_label(locale, "objection.direction", objection.get("direction", "?")),
                explanation=_clip(skip, _ASSERTION_MAX),
            )
        )
    for point in view.get("review_points") or []:
        if str(point).strip() and str(point).strip() != skip:
            lines.append(f"- {_clip(point, _ASSERTION_MAX)}")
    if not lines:
        return None
    return "\n".join([t(locale, "draft.review_points.header"), *lines])


def _warning_lines(view: dict[str, Any], locale: str) -> list[str]:
    flags: dict[str, Any] = view.get("flags") or {}
    checks: dict[str, Any] = view.get("checks") or {}
    critic: dict[str, Any] = view.get("critic") or {}
    out: list[str] = []

    def add(key: str, **kwargs: object) -> None:
        out.append(t(locale, f"draft.warning.{key}", **kwargs))

    for flag in ("downgraded", "post_deleted", "post_modified", "budget_incomplete"):
        if flags.get(flag):
            add(flag)
    if flags.get("cap_reached"):
        add("cap_reached")
    if flags.get("refused"):
        add("refused", roles=", ".join(str(r) for r in flags["refused"]))
    if flags.get("conflicting_sources"):
        add("conflicting_sources")
    if checks.get("invalid_quotes"):
        add("invalid_citations", count=len(checks["invalid_quotes"]))
    if checks.get("loaded_terms"):
        terms = ", ".join(
            str(h.get("term", h)) if isinstance(h, dict) else str(h) for h in checks["loaded_terms"]
        )
        add("loaded_terms", terms=terms)
    if checks.get("note_too_long"):
        add("note_too_long")
    if checks.get("unknown_links"):
        add("unknown_links", count=len(checks["unknown_links"]))
    if checks.get("sources_after_post"):
        add("sources_after_post", count=len(checks["sources_after_post"]))
    if checks.get("sources_too_old"):
        add("sources_too_old", count=len(checks["sources_too_old"]))
    if critic.get("contains_opinion"):
        add("critic_opinion")
    if critic.get("contains_generalization"):
        add("critic_generalization")
    if critic.get("judges_author"):
        add("critic_judges_author")
    if critic.get("loaded_words"):
        add("critic_loaded_words", words=", ".join(str(w) for w in critic["loaded_words"]))
    if critic.get("unsourced_facts"):
        add("critic_unsourced", count=len(critic["unsourced_facts"]))
    if critic.get("comments"):
        add("critic_comment", text=_clip(critic["comments"], _ASSERTION_MAX))
    return out


def render_draft(view: dict[str, Any], locale: str) -> list[str]:
    """Message parts of a draft for the reviewers, in the order of docs/factcheck/contexte.md:
    header, claim and post link, proposed note, rationale, what the sources do not say,
    points to check first (objection highlighted), warnings."""
    blocks: list[str | None] = [
        _header(view, locale),
        _claim_block(view, locale),
        _group_block(view, locale),
        _narrative_block(view, locale),
        _coordination_block(view, locale),
        _fact_block(view, locale),
        _external_block(view, locale),
        _note_block(view, locale),
        _rationale_block(view, locale),
        _list_block(t(locale, "draft.not_in_sources.header"), view.get("not_in_sources") or []),
        _review_points_block(view, locale),
    ]
    warnings = _warning_lines(view, locale)
    if warnings:
        blocks.append("\n".join([t(locale, "draft.warnings.header"), *warnings]))
    parts = _pack([b for b in blocks if b], MAX_MESSAGE)
    return parts[:_MAX_PARTS]


# --- review actions (buttons and modals) ---------------------------------------------


def _review_reply(locale: str, draft_id: str, result: dict[str, Any]) -> Reply:
    outcome = str(result.get("outcome"))
    draft = result.get("draft") or {}
    text = t(
        locale,
        f"draft.review.{outcome}",
        short_id=short(draft_id),
        veracity=veracity_label(locale, str(draft.get("veracity"))),
    )
    return Reply(text, ephemeral=True, final=outcome in ("validated", "rejected"))


@guarded
async def review_accept(gate: GateClient, acting: int, locale: str, draft_id: str) -> Reply:
    """Validate button: accept the draft as proposed."""
    return _review_reply(
        locale, draft_id, await gate.review_draft(acting, draft_id, decision="accept")
    )


@guarded
async def review_edit(
    gate: GateClient,
    acting: int,
    locale: str,
    draft_id: str,
    veracity: str | None,
    note_text: str | None,
    reason: str | None,
) -> Reply:
    """Correct modal: new veracity and/or note (in the member's language: fr or en)."""
    value: str | None = None
    if veracity and veracity.strip():
        value = parse_veracity(locale, veracity)
        if value is None:
            values = ", ".join(veracity_label(locale, v) for v in VERACITIES)
            return ok(t(locale, "draft.bad_veracity", value=_clip(veracity, 40), values=values))
    note = (note_text or "").strip() or None
    result = await gate.review_draft(
        acting,
        draft_id,
        decision="edit",
        veracity=value,
        note_text=note,
        note_lang=locale if note else None,
        reason=(reason or "").strip() or None,
    )
    return _review_reply(locale, draft_id, result)


@guarded
async def review_reject(
    gate: GateClient, acting: int, locale: str, draft_id: str, reason: str | None
) -> Reply:
    """Reject modal: the reason is required (gate answers ``TWR-FCK-009`` without it)."""
    result = await gate.review_draft(
        acting, draft_id, decision="reject", reason=(reason or "").strip() or None
    )
    return _review_reply(locale, draft_id, result)


# --- group, fact and band actions (buttons, modals, commands) -------------------------


def _uuid_arg(value: str) -> str:
    """A full public id typed by a member, or a clear input error."""
    ref = value.strip()
    if not _UUID.fullmatch(ref):
        raise GateError(422, "TWR-GEN-001", "A full identifier is required")
    return ref.lower()


def _date_arg(value: str, what: str) -> str:
    ref = value.strip()
    if not _DATE.fullmatch(ref):
        raise GateError(422, "TWR-GEN-001", f"{what}: expected YYYY-MM-DD")
    return ref


async def _draft_group(gate: GateClient, acting: int, draft_id: str) -> str:
    view = await gate.get_draft(acting, draft_id)
    group = view.get("group")
    if not group:
        raise GateError(404, "TWR-FCK-020", "Claim group not found")
    return str(group["id"])


@guarded
async def group_merge_same(
    gate: GateClient, acting: int, locale: str, draft_id: str, other_group: str, reason: str | None
) -> Reply:
    """Same-group button: the GIVEN group is merged INTO the draft's group (gate
    refuses a direction that would lose a verdict)."""
    target = await _draft_group(gate, acting, draft_id)
    merged = await gate.merge_groups(
        acting,
        target,
        source_id=_uuid_arg(other_group),
        reason=(reason or "").strip() or "merged from the draft",
    )
    return ok(
        t(locale, "group.merged", group_id=merged["id"], members=len(merged.get("members", [])))
    )


@guarded
async def group_split_draft(
    gate: GateClient, acting: int, locale: str, draft_id: str, claim_ids: str, reason: str | None
) -> Reply:
    """Split button: the claims (comma separated public ids) leave the draft's group."""
    group_id = await _draft_group(gate, acting, draft_id)
    return await _split(gate, acting, locale, group_id, claim_ids, reason or "")


async def _split(
    gate: GateClient, acting: int, locale: str, group_id: str, claim_ids: str, reason: str
) -> Reply:
    ids = [_uuid_arg(c) for c in re.split(r"[,\s]+", claim_ids.strip()) if c]
    if not ids:
        raise GateError(422, "TWR-GEN-001", "No claim id given")
    new = await gate.split_group(
        acting, group_id, claim_ids=ids, reason=reason.strip() or "split from the draft"
    )
    return ok(t(locale, "group.split", group_id=new["id"], members=len(new.get("members", []))))


@guarded
async def group_split(
    gate: GateClient, acting: int, locale: str, group_id: str, claim_ids: str, reason: str
) -> Reply:
    return await _split(gate, acting, locale, _uuid_arg(group_id), claim_ids, reason)


@guarded
async def group_merge(
    gate: GateClient, acting: int, locale: str, target: str, source: str, reason: str
) -> Reply:
    merged = await gate.merge_groups(
        acting, _uuid_arg(target), source_id=_uuid_arg(source), reason=reason
    )
    return ok(
        t(locale, "group.merged", group_id=merged["id"], members=len(merged.get("members", [])))
    )


@guarded
async def group_show(gate: GateClient, acting: int, locale: str, group_id: str) -> Reply:
    group = await gate.get_group(acting, _uuid_arg(group_id))
    verdict = group.get("active_verdict")
    lines = [
        t(
            locale,
            "group.show.header",
            group_id=group["id"],
            level=group["level"],
            state=group["state"],
            members=len(group["members"]),
        )
    ]
    if group.get("merged_into"):
        lines.append(t(locale, "group.show.merged_into", group_id=group["merged_into"]))
    if verdict:
        lines.append(
            t(
                locale,
                "group.show.verdict",
                veracity=veracity_label(locale, str(verdict["veracity"])),
                expires=_day(verdict.get("expires_at")),
                rule=verdict.get("reuse_rule") or "-",
            )
        )
    fact = group.get("reference_fact")
    if fact:
        lines.append(
            t(
                locale,
                "group.show.fact",
                label=fact["label"],
                value=fact["value"],
                unit=fact["unit"],
                band=group.get("band") or "-",
                side=group.get("band_side") or "-",
            )
        )
    for m in group["members"][:8]:
        links = " ".join(f"<{c['url']}>" for c in m["contents"] if c.get("url"))
        lines.append(
            t(
                locale,
                "group.show.member",
                claim_id=m["claim_id"],
                text=_clip(m["text_fr"] if locale == "fr" else m["text_original"], 120),
                links=links,
            )
        )
    for e in group["journal"][:5]:
        lines.append(
            t(
                locale,
                "group.show.event",
                kind=e["kind"],
                actor=e["actor"],
                when=_day(e.get("created_at")),
            )
        )
    return ok(listing(locale, lines[0], lines[1:]))


def _day(value: object) -> str:
    return str(value)[:10] if value else "-"


_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_MIN_PREFIX = 6


def _fact_line(locale: str, f: dict[str, Any]) -> str:
    return t(
        locale,
        "fact.line",
        short_id=short(f["id"]),
        status=_label(locale, "fact.status", f["status"]),
        label=f["label"],
        value=f["value"],
        unit=f["unit"],
        start=f["period_start"],
        end=f["period_end"],
        territory=f["territory"],
    )


async def _find_fact(gate: GateClient, acting: int, ref: str) -> dict[str, Any]:
    """Fact by full id or unambiguous prefix of at least six characters."""
    ref = ref.strip().lower()
    if _UUID.fullmatch(ref):
        return await gate.get_fact(acting, ref)
    if len(ref) < _MIN_PREFIX:
        raise GateError(422, "TWR-GEN-001", f"Identifier too short (min {_MIN_PREFIX} characters)")
    rows = await gate.list_facts(acting, limit=500)
    matches = [r for r in rows if str(r["id"]).lower().startswith(ref)]
    if not matches:
        raise GateError(404, "TWR-FCK-025", "Reference fact not found")
    if len(matches) > 1:
        raise _AmbiguousRef(ref)
    return matches[0]


class _AmbiguousRef(Exception):
    def __init__(self, ref: str) -> None:
        super().__init__(ref)
        self.ref = ref


@guarded
async def fact_show(gate: GateClient, acting: int, locale: str, fact_id: str) -> Reply:
    try:
        f = await _find_fact(gate, acting, fact_id)
    except _AmbiguousRef as exc:
        return ok(t(locale, "common.ambiguous", ref=exc.ref))
    return ok(
        _fact_line(locale, f)
        + "\n"
        + t(
            locale,
            "fact.show.source",
            source=f["source_name"],
            url=f["source_url"],
            published=f["published_at"],
            next=f.get("next_publication") or "-",
        )
    )


@guarded
async def fact_list(gate: GateClient, acting: int, locale: str, status: str | None = None) -> Reply:
    rows = await gate.list_facts(acting, status=status or None, limit=50)
    if not rows:
        return ok(t(locale, "fact.list.empty"))
    return ok(
        listing(
            locale,
            t(locale, "fact.list.header", count=len(rows)),
            [_fact_line(locale, f) for f in rows],
        )
    )


@guarded
async def fact_validate(gate: GateClient, acting: int, locale: str, fact_id: str) -> Reply:
    try:
        found = await _find_fact(gate, acting, fact_id)
    except _AmbiguousRef as exc:
        return ok(t(locale, "common.ambiguous", ref=exc.ref))
    f = await gate.validate_fact(acting, str(found["id"]))
    return ok(t(locale, "fact.validated", short_id=short(f["id"])))


@guarded
async def fact_reject(
    gate: GateClient, acting: int, locale: str, fact_id: str, reason: str
) -> Reply:
    try:
        found = await _find_fact(gate, acting, fact_id)
    except _AmbiguousRef as exc:
        return ok(t(locale, "common.ambiguous", ref=exc.ref))
    f = await gate.reject_fact(acting, str(found["id"]), reason=reason)
    return ok(t(locale, "fact.rejected", short_id=short(f["id"])))


@guarded
async def fact_create(
    gate: GateClient,
    acting: int,
    locale: str,
    draft_id: str,
    value: str,
    period: str,
    source_url: str,
    source_name: str,
    published_at: str,
) -> Reply:
    """ "Create fact" button: only once the verdict is validated (active)."""
    view = await gate.get_draft(acting, draft_id)
    if view.get("status") != "active":
        return ok(t(locale, "fact.needs_validated"))
    dates = [d for d in re.split(r"[\s/]+|\.\.", period.strip()) if d]
    if len(dates) == 1:
        dates = [dates[0], dates[0]]
    if len(dates) != 2:
        raise GateError(422, "TWR-GEN-001", "Period: expected 'YYYY-MM-DD YYYY-MM-DD'")
    created = await gate.create_fact_from_verdict(
        acting,
        verdict_id=draft_id,
        value=value.strip().replace(",", "."),
        period_start=_date_arg(dates[0], "Period start"),
        period_end=_date_arg(dates[1], "Period end"),
        source_url=source_url.strip(),
        source_name=source_name.strip(),
        published_at=_date_arg(published_at, "Published date"),
    )
    return ok(t(locale, "fact.created", short_id=short(created["id"]), label=created["label"]))


@guarded
async def band_validate(gate: GateClient, acting: int, locale: str, verdict_id: str) -> Reply:
    """ "Validate band" button and ``/fact band``: the verdict's outcome becomes the band's."""
    band = await gate.validate_band_from_verdict(acting, _uuid_arg(verdict_id))
    return ok(
        t(
            locale,
            "band.validated",
            label=_label(locale, "band.kind", band["band"]),
            side=band["side"],
            veracity=veracity_label(locale, str(band["veracity"])),
        )
    )


@guarded
async def request(gate: GateClient, acting: int, locale: str, url: str) -> Reply:
    """``/factcheck <url>``: queue a manual fact-check."""
    accepted = await gate.request_factcheck(acting, url=url.strip())
    key = "factcheck.duplicate" if accepted.get("duplicate") else "factcheck.accepted"
    return ok(
        t(
            locale,
            key,
            platform=accepted.get("platform", "?"),
            external_id=accepted.get("external_id", "?"),
        )
    )


# --- posting and reconciliation ------------------------------------------------------


async def post_draft(
    gate: GateClient,
    send: SendDraft,
    view: dict[str, Any],
    locale: str,
    *,
    channel: str = "dossiers",
) -> bool:
    """Post one draft with its buttons, then mark it notified. ``True`` when posted."""
    draft_id = str(view["draft_id"])
    posted = await send(channel, render_draft(view, locale), draft_id)
    if not posted:
        return False
    try:
        await gate.mark_draft_notified(None, draft_id)
    except GateError as exc:
        # Posted but not marked: the next reconciliation may post it a second time.
        log.warning("intercom.draft_mark_failed", code=exc.code, status=exc.status)
    return True


async def reconcile_drafts(
    gate: GateClient,
    post: Callable[[dict[str, Any]], Awaitable[bool]],
    *,
    page_size: int = 25,
    max_pages: int = 20,
) -> int:
    """Post every pending draft that was never displayed (oldest first); return how many.

    Covers the events missed while the bot was down and the very first start (no cursor).
    The pages are read first (``before_id`` pagination, newest first) so marking a draft
    notified never shifts a page; the number of pages is bounded."""
    pending: list[dict[str, Any]] = []
    before: int | None = None
    for _ in range(max_pages):
        page = await gate.list_drafts(None, notified=False, limit=page_size, before_id=before)
        pending.extend(page)
        if len(page) < page_size:
            break
        before = min(int(d["seq"]) for d in page)
    posted = 0
    for view in reversed(pending):
        try:
            if await post(view):
                posted += 1
        except GateError:
            raise
        except Exception as exc:
            capture(ec.BOT_DELIVERY_FAILED, exc, topic="draft.ready", draft_id=view.get("draft_id"))
    log.info("intercom.drafts_reconciled", pending=len(pending), posted=posted)
    return posted
