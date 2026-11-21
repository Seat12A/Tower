"""``/badge`` cards: Seat 12A verification badges and affiliations (gate client only).

Accounts are always identified by platform + native id (X numeric id), never by a
handle. Gate decides every permission (``badges:read``, ``badges:manage``).

Custom ids (persistent, see ``commands/badges.py``):
- ``t12a:bdg:<action>:<platform>:<external_id>``: account card actions;
- ``t12a:bdga:<action>:<uuid>``: affiliation; ``t12a:bdgr:<action>:<uuid>``: request;
  ``t12a:bdgp:<action>:<uuid>``: report;
- ``t12a:bdgq:<view>``: queue tabs; ``t12a:bdgo:<view>``: "open an item" select;
- ``t12a:bdgk:<platform>:<external_id>``: "revoke a key" select.
"""

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
    error_card,
    fmt_dt,
    guarded,
    listing,
    resolve_ref,
    short,
    success,
)
from tower.intercom.i18n import t
from tower.intercom.ui import emojis

__all__ = [
    "CATEGORIES",
    "PLATFORMS",
    "QUEUE_VIEWS",
    "acid",
    "affiliate",
    "affiliation_act",
    "affiliation_show",
    "badge_end",
    "badge_grant",
    "badge_history",
    "badge_imports",
    "badge_show",
    "badge_start_import",
    "check_account",
    "cid",
    "org_key_create",
    "org_key_revoke",
    "org_keys",
    "queue",
    "report_resolve",
    "report_show",
    "request_decide",
    "request_show",
    "resolve_account",
]

CATEGORIES = ("institutional", "media", "expert", "organization", "verified")
"""Priority order (highest first)."""
COLORS = {
    "institutional": "#F46600",
    "organization": "#E30B5C",
    "media": "#EA00FF",
    "expert": "#16A34A",
    "verified": "#335CFF",
}
AFFILIABLE = frozenset({"institutional", "media", "organization"})
PLATFORMS = ("x", "bluesky", "threads", "mastodon", "youtube", "tiktok", "telegram", "web")
QUEUE_VIEWS = ("requests", "reports", "affiliations")
REPORT_REASONS = ("wrong_category", "not_eligible", "impersonation", "wrong_affiliation", "other")
CID_MAX = 100
_LINES = 15
_ACCOUNT_ACCENTS: dict[str, Accent] = {"active": "primary", "blocked": "warning"}
_AFF_ACCENTS: dict[str, Accent] = {"active": "primary", "pending": "warning"}


# --- Custom ids ----------------------------------------------------------------------------


def cid(action: str, platform: str, external_id: str) -> str:
    """Account action id; empty when it would not fit (the slash commands still work)."""
    if platform not in PLATFORMS or not external_id or not external_id.isprintable():
        return ""
    value = f"t12a:bdg:{action}:{platform}:{external_id}"
    return value if len(value) <= CID_MAX else ""


def acid(kind: str, action: str, public_id: str) -> str:
    """``kind``: ``a`` (affiliation), ``r`` (request), ``p`` (report)."""
    return f"t12a:bdg{kind}:{action}:{public_id}"


def _account_label(acc: dict[str, Any]) -> str:
    handle = acc.get("handle")
    base = f"{acc['platform']}:{acc['external_id']}"
    return f"{base} (@{handle})" if handle else base


_X_HANDLE = re.compile(r"@?[A-Za-z0-9_]{1,15}")


def check_account(locale: str, platform: str, raw: str) -> tuple[str, str] | Reply:
    """Normalise a typed account without calling gate: an X numeric id, or an X handle
    (``@name``) to be converted by ``resolve_account``; anything else is refused."""
    plat = (platform or "x").strip().lower()
    value = raw.strip()
    if plat == "x":
        value = value.removeprefix("x:")
        if not value.isdigit() and not _X_HANDLE.fullmatch(value):
            return error_card(
                t(locale, "badge.bad_id.title"),
                t(locale, "badge.bad_id.body"),
                code_="TWR-ING-006",
                severity="warning",
            )
    return plat, value


async def resolve_account(
    gate: GateClient, acting: int, locale: str, platform: str, raw: str
) -> tuple[str, str, str] | Reply:
    """``(platform, native id, found)``. An X handle is converted once by gate with the X
    API (one billable read); ``found`` then says which account it is, so the member can
    check it before acting. Afterwards only the id is used."""
    checked = check_account(locale, platform, raw)
    if isinstance(checked, Reply):
        return checked
    plat, value = checked
    if plat != "x" or value.isdigit():
        return plat, value, ""
    hit = await gate.resolve_x_handle(acting, value)
    found = t(
        locale,
        "badge.resolved",
        handle=code("@" + str(hit.get("handle") or value.removeprefix("@"))),
        id=code(hit["external_id"]),
        name=hit.get("display_name") or "-",
    )
    return plat, str(hit["external_id"]), found


def _join(*parts: str) -> str:
    return "\n".join(p for p in parts if p)


def _category(locale: str, category: str | None) -> str:
    if not category:
        return "-"
    return t(
        locale,
        "badge.category.value",
        name=t(locale, f"badge.cat.{category}"),
        value=code(category),
        color=COLORS.get(category, "-"),
    )


# --- Account card ----------------------------------------------------------------------------


async def _account_card(
    gate: GateClient, acting: int, locale: str, platform: str, external_id: str, footer: str = ""
) -> Reply:
    try:
        badge: dict[str, Any] | None = await gate.get_badge(acting, platform, external_id)
    except GateError as exc:
        if exc.code != "TWR-BDG-001":
            raise
        badge = None
    member_affs = await gate.list_affiliations(acting, member=(platform, external_id), limit=10)
    status = badge["status"] if badge else "none"
    title = (badge or {}).get("label") or f"{platform}:{external_id}"
    details: list[tuple[str, str]] = [
        (t(locale, "badge.d.account"), code(f"{platform}:{external_id}")),
        (t(locale, "badge.d.status"), t(locale, f"badge.status.{status}")),
    ]
    if badge:
        if badge.get("handle"):
            details.append((t(locale, "badge.d.handle"), code("@" + badge["handle"])))
        details.append((t(locale, "badge.d.category"), _category(locale, badge.get("category"))))
        if badge.get("expertise"):
            details.append((t(locale, "badge.d.expertise"), badge["expertise"]))
        details.append((t(locale, "badge.d.source"), code(badge.get("source") or "-")))
        if badge.get("granted_by") and status == "active":
            details.append(
                (
                    t(locale, "badge.d.granted"),
                    t(
                        locale,
                        "badge.by",
                        by=code(badge["granted_by"]),
                        when=fmt_dt(badge.get("granted_at")),
                    ),
                )
            )
        if status != "active" and badge.get("ended_by"):
            details.append(
                (
                    t(locale, "badge.d.ended"),
                    t(
                        locale,
                        "badge.by",
                        by=code(badge["ended_by"]),
                        when=fmt_dt(badge.get("ended_at")),
                    ),
                )
            )
        if badge.get("wikidata_qid"):
            details.append((t(locale, "badge.d.wikidata"), code(badge["wikidata_qid"])))
        if badge.get("logo_url"):
            details.append((t(locale, "badge.d.logo"), badge["logo_url"]))
    body_parts: list[str] = []
    if badge and badge.get("end_reason") and status != "active":
        body_parts.append(t(locale, "badge.reason", reason=badge["end_reason"]))
    if badge and badge.get("note"):
        body_parts.append(t(locale, "badge.note", note=badge["note"]))
    if not badge:
        body_parts.append(t(locale, "badge.none"))
    items = tuple(
        t(
            locale,
            "badge.aff.item",
            org=a["org"].get("label") or _account_label(a["org"]),
            status=t(locale, f"badge.aff.status.{a['status']}"),
            primary=t(locale, "badge.aff.primary") if a.get("primary") else "",
            short_id=code(short(a["public_id"])),
            expires=fmt_dt(a.get("expires_at")),
        )
        for a in member_affs
        if a["status"] in ("pending", "active")
    )
    if items:
        body_parts.append(t(locale, "badge.aff.heading"))
    buttons = _account_buttons(locale, platform, external_id, badge)
    return card(
        title,
        "\n".join(body_parts),
        icon=emojis.VERIFIED_ORG
        if badge and badge.get("category") in AFFILIABLE
        else emojis.VERIFIED,
        accent=_ACCOUNT_ACCENTS.get(status, "neutral"),
        breadcrumb=(t(locale, "badge.title"),),
        details=tuple(details),
        items=items,
        footer=footer,
        buttons=buttons,
    )


def _account_buttons(
    locale: str, platform: str, external_id: str, badge: dict[str, Any] | None
) -> tuple[Button, ...]:
    status = badge["status"] if badge else "none"
    specs: list[tuple[str, str, str, str]] = []
    if status == "active":
        specs.append(("grant", "badge.btn.edit", emojis.EDIT, "primary"))
        specs.append(("revoke", "badge.btn.revoke", emojis.DELETE, "danger"))
        specs.append(("block", "badge.btn.block", emojis.BLACKLIST, "danger"))
    elif status == "blocked":
        specs.append(("unblock", "badge.btn.unblock", emojis.UNDONE, "primary"))
    else:
        specs.append(("grant", "badge.btn.grant", emojis.ADD, "success"))
        specs.append(("block", "badge.btn.block", emojis.BLACKLIST, "danger"))
    specs.append(("history", "badge.btn.history", emojis.HISTORY, "secondary"))
    if status == "active" and badge and badge.get("category") in AFFILIABLE:
        specs.append(("keys", "badge.btn.keys", emojis.KEY, "secondary"))
    return tuple(
        Button(t(locale, key), cid(action, platform, external_id), icon, style)  # type: ignore[arg-type]
        for action, key, icon, style in specs
        if cid(action, platform, external_id)
    )


def _back(locale: str, platform: str, external_id: str) -> tuple[Button, ...]:
    target = cid("show", platform, external_id)
    return (Button(t(locale, "ops.back"), target, emojis.BACK),) if target else ()


@guarded
async def badge_show(
    gate: GateClient, acting: int, locale: str, platform: str, external_id: str
) -> Reply:
    resolved = await resolve_account(gate, acting, locale, platform, external_id)
    if isinstance(resolved, Reply):
        return resolved
    checked, found = (resolved[0], resolved[1]), resolved[2]
    return await _account_card(gate, acting, locale, *checked, footer=found)


@guarded
async def badge_grant(
    gate: GateClient,
    acting: int,
    locale: str,
    platform: str,
    external_id: str,
    category: str,
    label: str | None = None,
    logo_url: str | None = None,
    note: str | None = None,
    expertise: str | None = None,
) -> Reply:
    resolved = await resolve_account(gate, acting, locale, platform, external_id)
    if isinstance(resolved, Reply):
        return resolved
    checked, found = (resolved[0], resolved[1]), resolved[2]
    done = await gate.grant_badge(
        acting,
        *checked,
        category=category,
        label=label,
        logo_url=logo_url,
        expertise=expertise,
        note=note,
    )
    footer = t(locale, "badge.granted", category=done["category"], version=done["version"])
    return await _account_card(gate, acting, locale, *checked, footer=_join(footer, found))


@guarded
async def badge_end(
    gate: GateClient,
    acting: int,
    locale: str,
    platform: str,
    external_id: str,
    action: str,
    reason: str | None = None,
) -> Reply:
    """``revoke``, ``block`` or ``unblock``."""
    resolved = await resolve_account(gate, acting, locale, platform, external_id)
    if isinstance(resolved, Reply):
        return resolved
    checked, found = (resolved[0], resolved[1]), resolved[2]
    await gate.badge_action(acting, *checked, action, reason=reason)
    return await _account_card(
        gate, acting, locale, *checked, footer=_join(t(locale, f"badge.done.{action}"), found)
    )


@guarded
async def badge_history(
    gate: GateClient, acting: int, locale: str, platform: str, external_id: str
) -> Reply:
    resolved = await resolve_account(gate, acting, locale, platform, external_id)
    if isinstance(resolved, Reply):
        return resolved
    checked = (resolved[0], resolved[1])
    rows = await gate.badge_history(acting, *checked)
    name = f"{checked[0]}:{checked[1]}"
    if not rows:
        return card(
            t(locale, "badge.history.title"),
            t(locale, "badge.history.empty"),
            icon=emojis.HISTORY,
            accent="neutral",
            breadcrumb=(t(locale, "badge.title"), name),
            buttons=_back(locale, *checked),
        )
    lines = [
        t(
            locale,
            "badge.history.item",
            action=t(locale, f"badge.action.{r['action']}"),
            by=code(r["actor"]),
            when=fmt_dt(r["created_at"]),
            detail=r.get("reason") or r.get("source") or "-",
        )
        for r in rows[:_LINES]
    ]
    items, more = listing(locale, lines)
    if not more and len(rows) > _LINES:
        more = t(locale, "common.more", count=len(rows) - _LINES)
    return card(
        t(locale, "badge.history.title"),
        icon=emojis.HISTORY,
        breadcrumb=(t(locale, "badge.title"), name),
        items=items,
        more=more,
        buttons=_back(locale, *checked),
    )


# --- Affiliations ------------------------------------------------------------------------------


def _affiliation_card(locale: str, a: dict[str, Any], footer: str = "") -> Reply:
    member, org = a["member"], a["org"]
    status = a["status"]
    details: list[tuple[str, str]] = [
        (t(locale, "badge.d.member"), code(_account_label(member))),
        (t(locale, "badge.d.org"), code(org.get("label") or _account_label(org))),
        (t(locale, "badge.d.status"), t(locale, f"badge.aff.status.{status}")),
        (t(locale, "badge.d.source"), code(a.get("source") or "-")),
    ]
    if a.get("title"):
        details.append((t(locale, "badge.d.title"), code(a["title"])))
    if a.get("primary"):
        details.append((t(locale, "badge.d.primary"), t(locale, "badge.yes")))
    if a.get("validated_by"):
        details.append(
            (
                t(locale, "badge.d.validated"),
                t(
                    locale,
                    "badge.by",
                    by=code(a["validated_by"]),
                    when=fmt_dt(a.get("validated_at")),
                ),
            )
        )
    if a.get("expires_at") and status == "active":
        details.append((t(locale, "badge.d.expires"), fmt_dt(a["expires_at"])))
    if a.get("wikidata_property"):
        details.append((t(locale, "badge.d.wikidata"), code(a["wikidata_property"])))
    body = t(locale, "badge.reason", reason=a["end_reason"]) if a.get("end_reason") else ""
    pid = str(a["public_id"])
    specs: list[tuple[str, str, str, str]] = []
    if status in ("pending", "expired"):
        specs.append(("validate", "badge.btn.validate", emojis.DONE, "success"))
    if status == "pending":
        specs.append(("reject", "badge.btn.reject", emojis.UNDONE, "danger"))
    if status == "active":
        specs.append(("reconfirm", "badge.btn.reconfirm", emojis.SYNC, "primary"))
        if not a.get("primary"):
            specs.append(("primary", "badge.btn.primary", emojis.STAR, "secondary"))
        specs.append(("revoke", "badge.btn.revoke_aff", emojis.DELETE, "danger"))
    buttons = [
        Button(t(locale, key), acid("a", action, pid), icon, style)  # type: ignore[arg-type]
        for action, key, icon, style in specs
    ]
    member_cid = cid("show", member["platform"], member["external_id"])
    if member_cid:
        buttons.append(Button(t(locale, "badge.btn.member"), member_cid, emojis.USER))
    org_cid = cid("show", org["platform"], org["external_id"])
    if org_cid:
        buttons.append(Button(t(locale, "badge.btn.org"), org_cid, emojis.BUSINESS))
    return card(
        t(locale, "badge.aff.title"),
        body,
        icon=emojis.GROUPS,
        accent=_AFF_ACCENTS.get(status, "neutral"),
        breadcrumb=(t(locale, "badge.title"), short(pid)),
        details=tuple(details),
        footer=footer or t(locale, "badge.aff.footer", short_id=short(pid)),
        buttons=tuple(buttons[:5]),
    )


@guarded
async def affiliate(
    gate: GateClient,
    acting: int,
    locale: str,
    platform: str,
    member: str,
    org: str,
    title: str | None = None,
    pending: bool = False,
) -> Reply:
    m = await resolve_account(gate, acting, locale, platform, member)
    if isinstance(m, Reply):
        return m
    o = await resolve_account(gate, acting, locale, platform, org)
    if isinstance(o, Reply):
        return o
    a = await gate.create_affiliation(
        acting, member=(m[0], m[1]), org=(o[0], o[1]), title=title, validate_now=not pending
    )
    key = "badge.aff.created_pending" if a["status"] == "pending" else "badge.aff.created"
    return _affiliation_card(locale, a, _join(t(locale, key), m[2], o[2]))


@guarded
async def affiliation_show(gate: GateClient, acting: int, locale: str, ref: str) -> Reply:
    pid = await resolve_ref(gate, acting, "affiliation", ref)
    return _affiliation_card(locale, await gate.get_affiliation(acting, pid))


@guarded
async def affiliation_act(
    gate: GateClient, acting: int, locale: str, ref: str, action: str, reason: str | None = None
) -> Reply:
    """``validate``, ``reconfirm``, ``reject``, ``revoke`` or ``primary``."""
    pid = await resolve_ref(gate, acting, "affiliation", ref)
    a = await gate.affiliation_action(acting, pid, action, reason=reason)
    return _affiliation_card(locale, a, t(locale, f"badge.aff.done.{action}"))


# --- Queue -------------------------------------------------------------------------------------


def _tabs(locale: str, current: str) -> tuple[Button, ...]:
    icons = {"requests": emojis.TICKET, "reports": emojis.FLAG, "affiliations": emojis.GROUPS}
    return tuple(
        Button(
            t(locale, f"badge.queue.tab.{view}"),
            f"t12a:bdgq:{view}",
            icons[view],
            "primary" if view == current else "secondary",
            disabled=view == current,
        )
        for view in QUEUE_VIEWS
    )


@guarded
async def queue(gate: GateClient, acting: int, locale: str, view: str = "requests") -> Reply:
    """Work to do: pending requests, open reports or pending affiliations."""
    view = view if view in QUEUE_VIEWS else "requests"
    options: list[Option] = []
    lines: list[str] = []
    if view == "requests":
        for r in await gate.list_badge_requests(acting):
            what = r.get("requested_category") or (r.get("org") or {}).get("external_id") or "-"
            lines.append(
                t(
                    locale,
                    "badge.queue.request",
                    kind=t(locale, f"badge.kind.{r['kind']}"),
                    account=code(_account_label(r["account"])),
                    what=code(what),
                    short_id=code(short(r["public_id"])),
                    when=fmt_dt(r.get("created_at")),
                )
            )
            options.append(
                Option(
                    str(r["public_id"]),
                    _account_label(r["account"])[:100],
                    description=f"{r['kind']} · {what}"[:100],
                )
            )
    elif view == "reports":
        for r in await gate.list_badge_reports(acting):
            lines.append(
                t(
                    locale,
                    "badge.queue.report",
                    reason=t(locale, f"badge.reason.{r['reason']}"),
                    account=code(_account_label(r["account"])),
                    short_id=code(short(r["public_id"])),
                    when=fmt_dt(r.get("created_at")),
                )
            )
            options.append(
                Option(
                    str(r["public_id"]),
                    _account_label(r["account"])[:100],
                    description=r["reason"][:100],
                )
            )
    else:
        for a in await gate.list_affiliations(acting, status="pending"):
            org = a["org"].get("label") or _account_label(a["org"])
            lines.append(
                t(
                    locale,
                    "badge.queue.affiliation",
                    member=code(_account_label(a["member"])),
                    org=code(org),
                    source=a.get("source") or "-",
                    short_id=code(short(a["public_id"])),
                )
            )
            options.append(
                Option(
                    str(a["public_id"]),
                    _account_label(a["member"])[:100],
                    description=f"→ {org}"[:100],
                )
            )
    items, more = listing(locale, lines)
    fields: tuple[Field, ...] = ()
    if options:
        fields = (
            Field(
                t(locale, "badge.queue.open"),
                Select(
                    f"t12a:bdgo:{view}",
                    placeholder=t(locale, "badge.queue.open.placeholder"),
                    options=tuple(options[:25]),
                ),
            ),
        )
    return card(
        t(locale, f"badge.queue.title.{view}"),
        "" if lines else t(locale, "badge.queue.empty"),
        icon=emojis.CHECKLIST,
        accent="primary" if lines else "neutral",
        breadcrumb=(t(locale, "badge.title"),),
        items=items,
        more=more,
        fields=fields,
        buttons=_tabs(locale, view),
    )


def _queue_back(locale: str, view: str) -> Button:
    return Button(t(locale, "badge.btn.queue"), f"t12a:bdgq:{view}", emojis.BACK)


def _request_card(locale: str, r: dict[str, Any], footer: str = "") -> Reply:
    acc = r["account"]
    details: list[tuple[str, str]] = [
        (t(locale, "badge.d.kind"), t(locale, f"badge.kind.{r['kind']}")),
        (t(locale, "badge.d.account"), code(_account_label(acc))),
        (t(locale, "badge.d.status"), t(locale, f"badge.req.status.{r['status']}")),
    ]
    if r.get("requested_category"):
        details.append((t(locale, "badge.d.category"), _category(locale, r["requested_category"])))
    if r.get("org"):
        details.append((t(locale, "badge.d.org"), code(_account_label(r["org"]))))
    if r.get("title"):
        details.append((t(locale, "badge.d.title"), code(r["title"])))
    if r.get("contact"):
        details.append((t(locale, "badge.d.contact"), code(r["contact"])))
    details.append((t(locale, "badge.d.submitted"), fmt_dt(r.get("created_at"))))
    if r.get("decided_by"):
        details.append(
            (
                t(locale, "badge.d.decided"),
                t(locale, "badge.by", by=code(r["decided_by"]), when=fmt_dt(r.get("decided_at"))),
            )
        )
    sections = []
    if r.get("justification"):
        sections.append(t(locale, "badge.req.justification", text=r["justification"][:1500]))
    if r.get("evidence_urls"):
        sections.append(
            t(locale, "badge.req.evidence", urls="\n".join(f"- {u}" for u in r["evidence_urls"]))
        )
    pid = str(r["public_id"])
    buttons: list[Button] = []
    if r["status"] == "pending":
        buttons += [
            Button(
                t(locale, "badge.btn.approve"), acid("r", "approve", pid), emojis.DONE, "success"
            ),
            Button(
                t(locale, "badge.btn.reject"), acid("r", "reject", pid), emojis.UNDONE, "danger"
            ),
        ]
    account_cid = cid("show", acc["platform"], acc["external_id"])
    if account_cid:
        buttons.append(Button(t(locale, "badge.btn.account"), account_cid, emojis.USER))
    buttons.append(_queue_back(locale, "requests"))
    return card(
        t(locale, "badge.req.title"),
        icon=emojis.TICKET,
        accent="warning" if r["status"] == "pending" else "neutral",
        breadcrumb=(t(locale, "badge.title"), short(pid)),
        details=tuple(details),
        sections=tuple(sections),
        footer=footer
        or (t(locale, "badge.req.note", note=r["decision_note"]) if r.get("decision_note") else ""),
        buttons=tuple(buttons),
    )


@guarded
async def request_show(gate: GateClient, acting: int, locale: str, ref: str) -> Reply:
    pid = await resolve_ref(gate, acting, "badge_request", ref)
    return _request_card(locale, await gate.get_badge_request(acting, pid))


@guarded
async def request_decide(
    gate: GateClient,
    acting: int,
    locale: str,
    ref: str,
    approve: bool,
    category: str | None = None,
    note: str | None = None,
    expertise: str | None = None,
) -> Reply:
    pid = await resolve_ref(gate, acting, "badge_request", ref)
    await gate.decide_badge_request(
        acting, pid, approve=approve, category=category, note=note, expertise=expertise
    )
    r = await gate.get_badge_request(acting, pid)
    key = "badge.req.approved" if approve else "badge.req.rejected"
    return _request_card(locale, r, t(locale, key))


def _report_card(locale: str, r: dict[str, Any], footer: str = "") -> Reply:
    acc = r["account"]
    details: tuple[tuple[str, str], ...] = (
        (t(locale, "badge.d.account"), code(_account_label(acc))),
        (t(locale, "badge.d.reason"), t(locale, f"badge.reason.{r['reason']}")),
        (t(locale, "badge.d.status"), t(locale, f"badge.rep.status.{r['status']}")),
        (t(locale, "badge.d.submitted"), fmt_dt(r.get("created_at"))),
    )
    if r.get("resolved_by"):
        details += (
            (
                t(locale, "badge.d.decided"),
                t(locale, "badge.by", by=code(r["resolved_by"]), when=fmt_dt(r.get("resolved_at"))),
            ),
        )
    pid = str(r["public_id"])
    buttons: list[Button] = []
    if r["status"] == "open":
        buttons += [
            Button(
                t(locale, "badge.btn.resolve"), acid("p", "resolve", pid), emojis.DONE, "success"
            ),
            Button(t(locale, "badge.btn.dismiss"), acid("p", "dismiss", pid), emojis.UNDONE),
        ]
    account_cid = cid("show", acc["platform"], acc["external_id"])
    if account_cid:
        buttons.append(Button(t(locale, "badge.btn.account"), account_cid, emojis.USER))
    buttons.append(_queue_back(locale, "reports"))
    return card(
        t(locale, "badge.rep.title"),
        t(locale, "badge.rep.comment", comment=r["comment"]) if r.get("comment") else "",
        icon=emojis.FLAG,
        accent="warning" if r["status"] == "open" else "neutral",
        breadcrumb=(t(locale, "badge.title"), short(pid)),
        details=details,
        footer=footer,
        buttons=tuple(buttons),
    )


@guarded
async def report_show(gate: GateClient, acting: int, locale: str, ref: str) -> Reply:
    pid = await resolve_ref(gate, acting, "badge_report", ref)
    return _report_card(locale, await gate.get_badge_report(acting, pid))


@guarded
async def report_resolve(
    gate: GateClient, acting: int, locale: str, ref: str, status: str, note: str | None = None
) -> Reply:
    """``status``: ``resolved`` (action taken) or ``dismissed``."""
    pid = await resolve_ref(gate, acting, "badge_report", ref)
    r = await gate.resolve_badge_report(acting, pid, status=status, note=note)
    return _report_card(locale, r, t(locale, f"badge.rep.done.{status}"))


# --- Organisation keys ----------------------------------------------------------------------


async def _keys_card(
    gate: GateClient, acting: int, locale: str, platform: str, external_id: str, footer: str = ""
) -> Reply:
    keys = await gate.list_org_keys(acting, platform, external_id)
    live = [k for k in keys if not k.get("revoked_at")]
    lines = [
        t(
            locale,
            "badge.keys.item",
            label=k.get("label") or "-",
            short_id=code(short(k["public_id"])),
            by=code(k["created_by"]),
            when=fmt_dt(k["created_at"]),
            state=t(locale, "badge.keys.revoked") if k.get("revoked_at") else "",
        )
        for k in keys
    ]
    items, more = listing(locale, lines)
    fields: tuple[Field, ...] = ()
    revoke_cid = f"t12a:bdgk:{platform}:{external_id}"
    if live and cid("keys", platform, external_id) and len(revoke_cid) <= CID_MAX:
        fields = (
            Field(
                t(locale, "badge.keys.revoke"),
                Select(
                    revoke_cid,
                    placeholder=t(locale, "badge.keys.revoke.placeholder"),
                    options=tuple(
                        Option(
                            str(k["public_id"]),
                            (k.get("label") or short(k["public_id"]))[:100],
                            description=short(k["public_id"]),
                        )
                        for k in live[:25]
                    ),
                ),
                help=t(locale, "badge.keys.revoke.help"),
            ),
        )
    buttons = tuple(
        b
        for b in (
            Button(
                t(locale, "badge.btn.newkey"),
                cid("newkey", platform, external_id),
                emojis.ADD,
                "success",
            ),
            *_back(locale, platform, external_id),
        )
        if b.custom_id
    )
    return card(
        t(locale, "badge.keys.title"),
        t(locale, "badge.keys.body") if keys else t(locale, "badge.keys.empty"),
        icon=emojis.KEY,
        breadcrumb=(t(locale, "badge.title"), f"{platform}:{external_id}"),
        items=items,
        more=more,
        fields=fields,
        footer=footer,
        buttons=buttons,
    )


@guarded
async def org_keys(
    gate: GateClient, acting: int, locale: str, platform: str, external_id: str
) -> Reply:
    resolved = await resolve_account(gate, acting, locale, platform, external_id)
    if isinstance(resolved, Reply):
        return resolved
    checked, found = (resolved[0], resolved[1]), resolved[2]
    return await _keys_card(gate, acting, locale, *checked, footer=found)


@guarded
async def org_key_create(
    gate: GateClient,
    acting: int,
    locale: str,
    platform: str,
    external_id: str,
    label: str | None = None,
) -> Reply:
    """New key: the token is shown once, in this private card only."""
    resolved = await resolve_account(gate, acting, locale, platform, external_id)
    if isinstance(resolved, Reply):
        return resolved
    checked = (resolved[0], resolved[1])
    key = await gate.create_org_key(acting, *checked, label=label)
    return success(
        t(locale, "badge.keys.created.title"),
        t(locale, "badge.keys.created.body", token=f"`{key['token']}`"),
        details=((t(locale, "badge.d.key"), code(short(key["public_id"]))),),
        footer=t(locale, "badge.keys.created.footer"),
        buttons=_back(locale, *checked),
        ephemeral=True,
    )


@guarded
async def org_key_revoke(
    gate: GateClient, acting: int, locale: str, platform: str, external_id: str, key_ref: str
) -> Reply:
    resolved = await resolve_account(gate, acting, locale, platform, external_id)
    if isinstance(resolved, Reply):
        return resolved
    checked = (resolved[0], resolved[1])
    pid = await resolve_ref(gate, acting, "org_key", key_ref)
    await gate.revoke_org_key(acting, pid)
    return await _keys_card(
        gate,
        acting,
        locale,
        *checked,
        footer=t(locale, "badge.keys.revoked_done", short_id=short(pid)),
    )


# --- Wikidata imports ---------------------------------------------------------------------------


def _runs_items(locale: str, runs: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(
        t(
            locale,
            "badge.import.item",
            scope=code(r["scope"]),
            started=fmt_dt(r.get("started_at")),
            listed=fmt_dt(r.get("listed_at")),
            accounts=r.get("accounts", 0),
            batches=r.get("batches", 0),
        )
        for r in runs
    )


@guarded
async def badge_imports(gate: GateClient, acting: int, locale: str) -> Reply:
    runs = await gate.list_badge_imports(acting)
    return card(
        t(locale, "badge.import.title"),
        "" if runs else t(locale, "badge.import.empty"),
        icon=emojis.SYNC,
        breadcrumb=(t(locale, "badge.title"),),
        items=_runs_items(locale, runs),
    )


@guarded
async def badge_start_import(
    gate: GateClient, acting: int, locale: str, scopes: str | None = None
) -> Reply:
    wanted = [s for s in (scopes or "").replace(",", " ").split() if s]
    done = await gate.start_badge_import(acting, wanted or None)
    runs = await gate.list_badge_imports(acting)
    return success(
        t(locale, "badge.import.started.title"),
        t(locale, "badge.import.started.body", scopes=", ".join(done.get("scopes", []))),
        items=_runs_items(locale, runs),
    )
