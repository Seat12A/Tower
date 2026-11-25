"""Forum of cases: one forum post per case, rendered as pure data.

A post has a **starter message** (the only control panel: status, claim, proposed
verdict and note, alert counts, the decision buttons and one "More actions" menu) and a
**thread** holding fixed sections in a fixed order (Evidence, Gaps & checks, Quality
warnings, Context) followed by one activity line per event. Tags (moderated, set by the
bot only) carry the status, the proposed verdict, the level and up to two signals.

Everything here is public, so it is rendered in English (``PUBLIC_LOCALE``). Icons
follow the all-or-nothing rule: an element type (action bar, menu options, section
titles, status titles, activity lines) shows icons only when every icon of that type
exists in ``ui.emojis``.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

from tower.intercom.handlers import (
    MAX_MESSAGE,
    Button,
    Option,
    Reply,
    Select,
    case_short,
    short,
)
from tower.intercom.handlers import factcheck as fc
from tower.intercom.i18n import has, t
from tower.intercom.ui import emojis

__all__ = [
    "PUBLIC_LOCALE",
    "TAGS",
    "Post",
    "activity_line",
    "all_or_none",
    "case_post",
    "draft_post",
    "draft_sections",
    "status_of",
]

PUBLIC_LOCALE = "en"
"""Public messages (channels, forum) are always in English: readers are unknown."""

NAME_MAX = 100
"""Discord limit of a forum post title."""
MAX_TAGS = 5
"""Discord limit of tags per forum post."""

STATUSES = ("to_review", "endorsed", "validated", "published", "rejected", "superseded")
LEVELS = ("N1", "N2", "N3", "N4")
SIGNALS = ("attention", "wave", "post_deleted")

TAGS: tuple[str, ...] = (
    *(f"status.{s}" for s in STATUSES),
    *(f"verdict.{v}" for v in fc.VERACITIES),
    *(f"level.{lv}" for lv in LEVELS),
    *(f"signal.{s}" for s in SIGNALS),
)
"""Tag keys of the forum (18 of the 20 Discord allows); labels: ``forum.tag.<key>``."""

_FINAL_STATUSES = frozenset({"published", "rejected", "superseded"})
"""A post in one of these statuses is archived (still writable)."""

_STATUS_ICONS = {
    "to_review": emojis.PENDING,
    "endorsed": emojis.ENDORSED,
    "validated": emojis.DONE,
    "published": emojis.PUBLISH,
    "rejected": emojis.UNDONE,
    "superseded": emojis.MERGE,
}
_STATUS_ACCENTS = {
    "to_review": "primary",
    "endorsed": "primary",
    "validated": "success",
    "published": "success",
    "rejected": "neutral",
    "superseded": "neutral",
}
_MENU_ICONS = {
    "samegroup": emojis.MERGE,
    "split": emojis.SPLIT,
    "fact": emojis.BOOK,
    "band": emojis.BAND,
    "notes": emojis.TRANSLATE,
}
_SECTION_ICONS = {
    "evidence": emojis.EVIDENCE,
    "checks": emojis.CHECKLIST,
    "warnings": emojis.WARNING,
    "context": emojis.CONTEXT,
}
_ACTIVITY_ICONS = {
    "endorsed": emojis.ENDORSED,
    "validated": emojis.DONE,
    "corrected": emojis.EDIT,
    "rejected": emojis.UNDONE,
    "published": emojis.PUBLISH,
    "transitioned": emojis.HISTORY,
}

MORE_ACTIONS = ("samegroup", "split", "fact", "band")
"""Options of the "More actions" menu (``t12a:fc:more:<draft id>``)."""
_DECIDED_ACTIONS = ("fact", "band")
"""Menu options kept once the draft is validated (fact and band need a verdict)."""
LEGACY_MENU_VALUES = ("group", "narrative")
"""Options of menus posted before the info buttons existed: still answered."""

INFO_KINDS = ("quotes", "context", "notes")
"""Read-only details shown on demand, to the clicker only (``t12a:fc:info:<kind>:<id>``)."""
_INFO_ICONS = {"quotes": emojis.QUOTE, "context": emojis.CONTEXT}
"""Icons of the info buttons of the thread (all or none)."""


def all_or_none(icons: dict[str, str]) -> dict[str, str]:
    """The icons of one element type, or none of them when one is missing."""
    return icons if all(icons.values()) else dict.fromkeys(icons, "")


def info_custom_id(kind: str, draft_id: str) -> str:
    return f"t12a:fc:info:{kind}:{draft_id}"


def _t(key: str, **kwargs: object) -> str:
    return t(PUBLIC_LOCALE, key, **kwargs)


@dataclass(frozen=True, slots=True)
class Post:
    """A forum post: title, tag keys (``TAGS``), starter card, archived or not."""

    name: str
    tags: tuple[str, ...]
    starter: Reply
    archived: bool = False


# --- status and tags -------------------------------------------------------------------


def status_of(case_status: object, draft_status: object = None) -> str:
    """Forum status of a case (and of its draft when it has one)."""
    if draft_status == "superseded":
        return "superseded"
    return {
        "received": "to_review",
        "in_review": "endorsed",
        "validated": "validated",
        "published": "published",
        "rejected": "rejected",
    }.get(str(case_status), "to_review")


def _needs_attention(view: dict[str, Any]) -> bool:
    return bool(view.get("objection") or fc._warning_lines(view, PUBLIC_LOCALE))


def _tags(status: str, veracity: object, level: object, signals: list[str]) -> tuple[str, ...]:
    tags = [f"status.{status}"]
    if veracity in fc.VERACITIES:
        tags.append(f"verdict.{veracity}")
    if level in LEVELS:
        tags.append(f"level.{level}")
    tags += [f"signal.{s}" for s in signals]
    return tuple(tags[:MAX_TAGS])


TOPIC_WORDS = 3
"""Words of the narrative title kept in a post title."""


def _name(case_id: str, topic: object = None) -> str:
    """Post title: the case id, plus a few words of the narrative title when the
    narrative has one (already generated: no extra model call). Never the claim itself:
    it is quoted in the starter message."""
    name = _t("forum.post.name", id=case_id)
    words = str(topic or "").split()[:TOPIC_WORDS]
    if words:
        name = f"{name} · {' '.join(words)}"
    return name[:NAME_MAX]


# --- starter message -------------------------------------------------------------------


def _status_title(status: str, suffix: str = "") -> tuple[str, str]:
    icons = all_or_none(_STATUS_ICONS)
    title = _t(f"forum.status.{status}")
    return (f"{title} · {suffix}" if suffix else title), icons[status]


def _more_menu(draft_id: str, actions: tuple[str, ...]) -> Select:
    icons = all_or_none({a: _MENU_ICONS[a] for a in actions})
    return Select(
        custom_id=fc.custom_id("more", draft_id),
        placeholder=_t("forum.more.placeholder"),
        options=tuple(
            Option(
                a,
                _t(f"forum.more.{a}"),
                description=_t(f"forum.more.{a}.help"),
                icon=icons[a],
            )
            for a in actions
        ),
    )


def _bar(specs: list[tuple[str, str, str, str]], url: str | None) -> tuple[Button, ...]:
    """Action bar: ``(label key, custom id, icon, style)`` + the post link, icons all or
    none."""
    icons = [icon for _, _, icon, _ in specs] + ([emojis.LINK] if url else [])
    show = all(icons)
    buttons = [
        Button(_t(key), custom_id, icon=icon if show else "", style=style)  # type: ignore[arg-type]
        for key, custom_id, icon, style in specs
    ]
    if url:
        buttons.append(
            Button(
                _t("forum.button.open_post"),
                icon=emojis.LINK if show else "",
                style="link",
                url=url,
            ),
        )
    return tuple(buttons[:5])


def _verdict_line(view: dict[str, Any]) -> str:
    confidence = view.get("confidence")
    pct = f"{round(float(confidence) * 100)}%" if confidence is not None else "?"
    band = view.get("confidence_band")
    if band:
        pct = f"{pct} ({fc._label(PUBLIC_LOCALE, 'band', band)})"
    return _t(
        "forum.post.verdict",
        verdict=fc.veracity_label(PUBLIC_LOCALE, str(view.get("veracity"))),
        context=fc._label(PUBLIC_LOCALE, "context_need", view.get("context_need")),
        confidence=pct,
    )


def _claim(view: dict[str, Any]) -> tuple[str, str]:
    """``(claim text, language tag)``: the English original when there is one."""
    claim = view.get("claim") or {}
    if claim.get("lang") == "en" and claim.get("text_original"):
        return str(claim["text_original"]), ""
    text = claim.get("text_fr") or claim.get("text_original") or ""
    return str(text), _t("draft.claim.fr_tag")


def _note(view: dict[str, Any]) -> str:
    notes: dict[str, dict[str, Any]] = view.get("notes") or {}
    lang = next((c for c in ("en", "fr") if c in notes), next(iter(notes), None))
    if lang is None:
        return _t("draft.note.none")
    text = str(notes[lang].get("text", ""))
    header = _t("forum.post.note", lang=lang, length=len(text))
    return header + "\n" + fc._quote_block(text)


def _attention_line(view: dict[str, Any]) -> str:
    count = len(fc._warning_lines(view, PUBLIC_LOCALE))
    parts: list[str] = []
    if count:
        parts.append(_t("forum.post.warnings", count=count))
    if view.get("objection"):
        parts.append(_t("forum.post.objection"))
    if not parts:
        return ""
    icon = emojis.WARNING
    return f"{icon} " + " · ".join(parts) + " " + _t("forum.post.see_thread")


def draft_post(view: dict[str, Any], case_status: object = None) -> Post:
    """The post of a case that has a fact-check draft."""
    draft_id = str(view.get("draft_id", ""))
    case = view.get("case") or {}
    status = status_of(case_status or case.get("status"), view.get("status"))
    if status == "to_review" and view.get("status") == "active":
        status = "validated"
    if view.get("status") == "rejected":
        status = "rejected"
    level = str(view.get("level", "?"))
    suffix = level + (" " + _t("forum.reinforced") if view.get("depth") == "reinforced" else "")
    title, icon = _status_title(status, suffix)
    text, tag = _claim(view)
    blocks = [fc._quote_block(text) + (f"\n-# {tag.strip()}" if tag else ""), _verdict_line(view)]
    body = "\n\n".join(blocks)
    note = _note(view)
    attention = _attention_line(view)
    url = (view.get("content") or {}).get("url")
    narrative = view.get("narrative") or {}
    group = view.get("group") or {}
    ids = [_t("forum.footer.case", id=case_short(case, "id"))] if case else []
    ids.append(_t("forum.footer.draft", id=short(draft_id)))
    if group.get("id"):
        ids.append(_t("forum.footer.group", id=short(group["id"])))
    if narrative.get("id"):
        ids.append(_t("forum.footer.narrative", id=short(narrative["id"])))
    notes = ("notes",) if len(view.get("notes") or {}) > 1 else ()
    menu: Select | None
    if view.get("status") == "draft" and status not in _FINAL_STATUSES:
        bar = _bar(
            [
                (
                    "draft.button.validate",
                    fc.custom_id("validate", draft_id),
                    emojis.DONE,
                    "success",
                ),
                ("draft.button.correct", fc.custom_id("correct", draft_id), emojis.EDIT, "primary"),
                ("draft.button.reject", fc.custom_id("reject", draft_id), emojis.UNDONE, "danger"),
            ],
            url,
        )
        menu = _more_menu(draft_id, (*MORE_ACTIONS, *notes))
    elif status == "validated":
        bar = _bar(
            [
                (
                    "forum.button.publish",
                    fc.custom_id("publish", draft_id),
                    emojis.PUBLISH,
                    "success",
                ),
            ],
            url,
        )
        menu = _more_menu(draft_id, (*_DECIDED_ACTIONS, *notes))
    else:
        bar = _bar([], url)
        menu = _more_menu(draft_id, notes) if notes else None
    signals: list[str] = []
    if _needs_attention(view) and status not in _FINAL_STATUSES:
        signals.append("attention")
    if narrative.get("state") in ("surge", "alert"):
        signals.append("wave")
    if (view.get("flags") or {}).get("post_deleted"):
        signals.append("post_deleted")
    starter = Reply(
        title=title,
        icon=icon,
        accent=_STATUS_ACCENTS[status],  # type: ignore[arg-type]
        body=body,
        footer=" · ".join(ids),
        buttons=bar,
        menu=menu,
        sections=tuple(s for s in (note, attention) if s),
        ephemeral=False,
    )
    return Post(
        name=_name(case_short(case, "id"), narrative.get("title")),
        tags=_tags(status, view.get("veracity"), view.get("level"), signals[:2]),
        starter=starter,
        archived=status in _FINAL_STATUSES,
    )


def case_post(case: dict[str, Any]) -> Post:
    """The post of a case without a draft (manual case, moderation): title,
    platform, status. Its actions stay in ``/case``."""
    status = status_of(case.get("status"))
    title, icon = _status_title(status)
    case_id = case_short(case) if case.get("public_id") else case_short(case, "id")
    platform = str(case.get("platform") or ("x" if case.get("x_post_id") else "-"))
    x_post = case.get("x_post_id")
    url = f"https://x.com/i/status/{x_post}" if x_post else None
    starter = Reply(
        title=title,
        icon=icon,
        accent=_STATUS_ACCENTS[status],  # type: ignore[arg-type]
        body=fc._quote_block(str(case.get("title") or _t("forum.untitled"))),
        details=((_t("notify.label.platform"), f"`{platform}`"),),
        footer=_t("forum.footer.case", id=case_id),
        buttons=_bar([], url),
        ephemeral=False,
    )
    return Post(
        name=_name(case_id),
        tags=_tags(status, None, None, []),
        starter=starter,
        archived=status in _FINAL_STATUSES,
    )


# --- thread sections -------------------------------------------------------------------


_STANDINGS_SHOWN = frozenset(
    {
        "official",
        "academic",
        "journal",
        "press",
        "fact_checker",
        "reference",
        "organization",
        "user_generated",
        "commercial",
        "first_party",
    }
)
"""Source standings labelled in the evidence list (``unclear`` and unknown are not)."""


def _sources(view: dict[str, Any]) -> dict[str, tuple[int, str]]:
    """Cited sources, numbered once in order of first citation: ``url -> (n, name)``."""
    out: dict[str, tuple[int, str]] = {}
    for item in view.get("rationale") or []:
        for cite in item.get("citations") or []:
            src = cite.get("source") or {}
            url = str(src.get("url") or "")
            if cite.get("valid", True) and url and url not in out:
                name = str(src.get("publisher") or src.get("title") or url)
                standing = src.get("standing")
                if standing in _STANDINGS_SHOWN:
                    name = f"{fc._clip(name, 80)} · {_t(f'forum.standing.{standing}')}"
                out[url] = (len(out) + 1, name)
    return out


def _has_quotes(view: dict[str, Any]) -> bool:
    return any(
        cite.get("valid", True) and cite.get("quote")
        for item in view.get("rationale") or []
        for cite in item.get("citations") or []
    )


def _evidence(view: dict[str, Any]) -> str | None:
    """Condensed rationale: each assertion with its source references (``[S1]``), then
    the sources listed once. The quotes are behind the "Show quotes" button."""
    sources = _sources(view)
    lines: list[str] = []
    for item in view.get("rationale") or []:
        refs: list[str] = []
        for cite in item.get("citations") or []:
            url = str((cite.get("source") or {}).get("url") or "")
            if cite.get("valid", True) and url in sources:
                ref = f"[S{sources[url][0]}]"
                if ref not in refs:
                    refs.append(ref)
        assertion = fc._clip(item.get("assertion", ""), fc._ASSERTION_MAX)
        lines.append(f"- {assertion}" + (f" {''.join(refs)}" if refs else ""))
    if not lines:
        return None
    if sources:
        lines.append("")
        lines.append(_t("forum.evidence.sources"))
        lines += [
            _t("forum.evidence.source", n=n, name=fc._clip(name, 120), url=url)
            for url, (n, name) in sources.items()
        ]
    return "\n".join(lines)


def _checks(view: dict[str, Any]) -> str | None:
    blocks = [
        fc._list_block(_t("draft.not_in_sources.header"), view.get("not_in_sources") or []),
        fc._review_points_block(view, PUBLIC_LOCALE),
    ]
    joined = "\n\n".join(b for b in blocks if b)
    return joined or None


def _warnings(view: dict[str, Any]) -> str | None:
    lines = fc._warning_lines(view, PUBLIC_LOCALE)
    return "\n".join(lines) if lines else None


def _context(view: dict[str, Any]) -> tuple[str, tuple[str, ...]] | None:
    """Context card: one short line per available item and the info buttons to open
    the details (``(body, info kinds)``). External fact-checks are short: shown here."""
    lines: list[str] = []
    details = bool(
        view.get("group")
        or view.get("narrative")
        or view.get("coordination")
        or view.get("reference_fact")
        or view.get("band")
    )
    group = view.get("group")
    if group:
        lines.append(
            _t(
                "forum.context.group",
                id=short(group.get("id", "?")),
                members=group.get("members", "?"),
                platforms=", ".join(view.get("platforms") or []) or "-",
                others=len(view.get("other_posts") or []),
            )
        )
    narrative = view.get("narrative")
    signals = len(view.get("coordination") or [])
    if narrative or signals:
        if narrative:
            state_key = f"narrative.state.{narrative.get('state')}"
            lines.append(
                _t(
                    "forum.context.narrative",
                    title=fc._clip(narrative.get("title") or _t("narrative.untitled"), 80),
                    state=_t(state_key)
                    if has(PUBLIC_LOCALE, state_key)
                    else narrative.get("state", "?"),
                )
            )
        if signals:
            lines.append(_t("forum.context.signals", count=signals))
    if view.get("reference_fact") or view.get("band"):
        key = "forum.context.fact_outdated" if view.get("outdated") else "forum.context.fact"
        lines.append(_t(key))
    external = fc._external_block(view, PUBLIC_LOCALE)
    if external:
        lines.append(external)
    if not lines:
        return None
    return "\n".join(lines), ("context",) if details else ()


def _info_buttons(draft_id: str, kinds: tuple[str, ...]) -> tuple[Button, ...]:
    icons = all_or_none(_INFO_ICONS)
    return tuple(
        Button(_t(f"forum.info.button.{k}"), info_custom_id(k, draft_id), icon=icons[k])
        for k in kinds
    )


def draft_sections(view: dict[str, Any]) -> list[Reply]:
    """The thread sections, in order; an empty section is omitted, a long one is split
    into "(cont.)" cards. Info buttons sit under the section they complete."""
    draft_id = str(view.get("draft_id", ""))
    icons = all_or_none(_SECTION_ICONS)
    context = _context(view)
    sections: list[tuple[str, str | None, tuple[Button, ...]]] = [
        (
            "evidence",
            _evidence(view),
            _info_buttons(draft_id, ("quotes",)) if _has_quotes(view) else (),
        ),
        ("checks", _checks(view), ()),
        ("warnings", _warnings(view), ()),
        (
            "context",
            context[0] if context else None,
            _info_buttons(draft_id, context[1]) if context else (),
        ),
    ]
    cards: list[Reply] = []
    for key, body, buttons in sections:
        if not body:
            continue
        accent = "warning" if key == "warnings" else "neutral"
        parts = fc._pack([body], MAX_MESSAGE - fc._CARD_OVERHEAD)[: fc._MAX_PARTS]
        for index, part in enumerate(parts):
            title = _t(f"forum.section.{key}")
            if index:
                title = _t("forum.section.cont", title=title)
            last = index == len(parts) - 1
            cards.append(
                Reply(
                    title=title,
                    icon=icons[key],
                    accent=accent,  # type: ignore[arg-type]
                    body=part,
                    buttons=buttons if last else (),
                    ephemeral=False,
                )
            )
    return cards


def info_reply(view: dict[str, Any], kind: str, locale: str) -> Reply:
    """Details behind an info button, for the clicker only, in their language."""
    blocks: list[str | None]
    if kind == "quotes":
        blocks = [fc._rationale_block(view, locale)]
    elif kind == "context":
        blocks = [
            fc._group_block(view, locale),
            fc._narrative_block(view, locale),
            fc._coordination_block(view, locale),
            fc._fact_block(view, locale),
        ]
    else:
        notes: dict[str, dict[str, Any]] = view.get("notes") or {}
        blocks = [
            t(locale, "forum.post.note", lang=lang, length=len(str(n.get("text", ""))))
            + "\n"
            + fc._quote_block(str(n.get("text", "")))
            for lang, n in notes.items()
        ]
    clean = [b for b in blocks if b]
    title = t(locale, f"forum.info.title.{kind}")
    icon = _MENU_ICONS["notes"] if kind == "notes" else all_or_none(_INFO_ICONS).get(kind, "")
    if not clean:
        return Reply(title=title, icon=icon, body=t(locale, "forum.info.empty"), accent="neutral")
    parts = fc._pack(clean, MAX_MESSAGE - fc._CARD_OVERHEAD)[: fc._MAX_PARTS]
    cards = [
        Reply(
            title=title if not index else t(locale, "forum.section.cont", title=title),
            icon=icon,
            accent="neutral",
            body=part,
        )
        for index, part in enumerate(parts)
    ]
    return dataclasses.replace(cards[0], extra=tuple(cards[1:]))


# --- activity --------------------------------------------------------------------------


def _who(actor: object) -> str:
    """``discord:<id>`` labels become a mention (rendered without a ping)."""
    label = str(actor or "")
    if label.startswith("discord:") and label[8:].isdigit():
        return f"<@{label[8:]}>"
    return f"`{label or '?'}`"


def activity_line(kind: str, actor: object = None, **details: object) -> Reply:
    """One line of the activity log of a post (``kind``: see ``_ACTIVITY_ICONS``)."""
    icons = all_or_none(_ACTIVITY_ICONS)
    icon = icons.get(kind, "")
    text = _t(f"forum.activity.{kind}", who=_who(actor), **details)
    extra = str(details.get("reason") or "").strip()
    if extra:
        text += " · " + _t("forum.activity.reason", reason=fc._clip(extra, 300))
    return Reply(body=f"{icon} {text}" if icon else text, accent="neutral", ephemeral=False)


def review_activity(payload: dict[str, Any], previous_veracity: object = None) -> Reply:
    """Activity line of a ``draft.reviewed`` event."""
    outcome = str(payload.get("outcome"))
    decision = str(payload.get("decision"))
    final = payload.get("final_veracity")
    if outcome == "rejected":
        kind = "rejected"
    elif decision == "edit":
        kind = "corrected"
    else:
        kind = "validated" if outcome == "validated" else "endorsed"
    verdict = fc.veracity_label(PUBLIC_LOCALE, str(final)) if final else "?"
    line = activity_line(kind, payload.get("reviewer"), verdict=verdict)
    if kind == "corrected" and outcome == "endorsed":
        line = dataclasses.replace(
            line, body=line.body + " · " + _t("forum.activity.awaiting_editor")
        )
    return line
