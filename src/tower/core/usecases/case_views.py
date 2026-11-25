"""Read models of cases for the website and the team: full details, search, statistics.

Read-only. Anonymous callers (``cases:read_public``) only see **published** cases and
**validated** verdicts; ``cases:read`` sees everything plus internal fields. Nothing here
ever returns the author of a post (rule 10): the site embeds the post from its link.
Ids are public UUIDs (internal ids never leave).
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, func, literal_column, or_, select
from sqlalchemy import case as sql_case
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain import error_codes as ec
from tower.core.domain.cases import CaseKind, CaseStatus
from tower.core.domain.errors import InvalidInput, NotFound
from tower.core.domain.factcheck.urls import content_url
from tower.core.domain.factcheck.values import VerdictStatus
from tower.core.domain.security import Actor, Permission
from tower.core.models.cases import Case, CaseEvent
from tower.core.models.factcheck import (
    Claim,
    ClaimGroup,
    ClaimGroupMember,
    ContentClaim,
    Verdict,
)
from tower.core.models.ingestion import ContentItem
from tower.core.usecases.drafts import PublicNote, norm_lang, public_note

__all__ = [
    "MAX_OFFSET",
    "CaseDetails",
    "CasePage",
    "CaseSearch",
    "CaseStats",
    "CaseSummary",
    "SearchSort",
    "StatsInterval",
    "case_stats",
    "get_case_details",
    "search_cases",
]

MAX_OFFSET = 10_000
"""Deepest row a page may start at: beyond, narrow the filters."""
MAX_QUERY = 200
TOP_JURISDICTIONS = 20
MAX_BUCKETS = 400
_SHOWN_VERDICTS = (
    VerdictStatus.ACTIVE.value,
    VerdictStatus.SUPERSEDED.value,
    VerdictStatus.EXPIRED.value,
)
_FRENCH: Any = literal_column("'french'::regconfig")
_SIMPLE: Any = literal_column("'simple'::regconfig")


class SearchSort(StrEnum):
    NEWEST = "newest"
    OLDEST = "oldest"
    RELEVANCE = "relevance"
    """Text rank of ``q`` (newest first without ``q``)."""


class StatsInterval(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


@dataclass(frozen=True, slots=True)
class CaseSearch:
    """Filters of ``search_cases`` and ``case_stats`` (all optional, combined with AND;
    repeated values of one filter are combined with OR)."""

    q: str | None = None
    kind: CaseKind | None = None
    status: CaseStatus | None = None
    veracity: tuple[str, ...] = ()
    claim_type: tuple[str, ...] = ()
    context_need: str | None = None
    lang: str | None = None
    jurisdiction: str | None = None
    platform: str | None = None
    since: datetime | None = None
    until: datetime | None = None


@dataclass(frozen=True, slots=True)
class CaseSummary:
    public_id: uuid.UUID
    kind: str
    title: str | None
    platform: str | None
    x_post_id: str | None
    external_id: str | None
    url: str | None
    published_url: str | None
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime
    claim_type: str | None
    claim_lang: str | None
    jurisdictions: list[str]
    veracity: str | None
    context_need: str | None
    verdict_state: str | None
    """``current`` (active, not expired), ``expired`` or ``superseded``; ``None`` without
    a validated verdict (internal callers: ``draft`` or ``rejected`` too)."""
    # Internal fields (cases:read only; None for the public)
    status: str | None = None
    priority: int | None = None
    assigned_to: str | None = None


@dataclass(frozen=True, slots=True)
class CasePage:
    items: list[CaseSummary]
    page: int
    per_page: int
    total: int
    pages: int


@dataclass(frozen=True, slots=True)
class CaseDetails:
    summary: CaseSummary
    content: dict[str, Any] | None
    claim: dict[str, Any] | None
    verdict: dict[str, Any] | None
    note: PublicNote | None
    posts_count: int
    """Posts carrying the same claim (claim group members with a content)."""
    timeline: list[dict[str, Any]]
    internal: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class CaseStats:
    total: int
    by_kind: dict[str, int]
    by_veracity: dict[str, int]
    by_claim_type: dict[str, int]
    by_lang: dict[str, int]
    by_jurisdiction: dict[str, int]
    timeline: list[dict[str, Any]]
    interval: str
    by_status: dict[str, int] | None = None
    """Internal callers only (the public sees published cases only)."""
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


# --- shared query ----------------------------------------------------------------------


def _internal(actor: Actor) -> bool:
    if actor.can(Permission.CASE_READ):
        return True
    actor.require(Permission.CASE_READ_PUBLIC)
    return False


def _published_at() -> Any:
    return (
        select(func.max(CaseEvent.created_at))
        .where(CaseEvent.case_id == Case.id, CaseEvent.to_status == CaseStatus.PUBLISHED.value)
        .correlate(Case)
        .scalar_subquery()
    )


def _visible_verdict() -> ColumnElement[bool]:
    """A verdict the public may see: validated by the team (never a draft or a rejection)."""
    return and_(Verdict.validated_at.is_not(None), Verdict.status.in_(_SHOWN_VERDICTS))


def _joined(stmt: Select[Any], internal: bool) -> Select[Any]:
    """Case -> its verdict (the draft it was opened for) -> group -> representative claim,
    and its content. For the public the verdict join keeps validated verdicts only, so a
    draft never leaks through a filter or a count."""
    on = Verdict.id == Case.verdict_id
    if not internal:
        on = and_(on, _visible_verdict())
    stmt = (
        stmt.outerjoin(Verdict, on)
        .outerjoin(ClaimGroup, ClaimGroup.id == Verdict.claim_group_id)
        .outerjoin(Claim, Claim.id == ClaimGroup.representative_claim_id)
        .outerjoin(ContentItem, ContentItem.id == Case.content_id)
    )
    if not internal:
        stmt = stmt.where(Case.status == CaseStatus.PUBLISHED.value)
    return stmt


def _searched_text() -> Any:
    return func.coalesce(Case.title, "") + " " + func.coalesce(Claim.text_fr, "")


def _french_vector() -> Any:
    return func.to_tsvector(_FRENCH, _searched_text())


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _query_text(q: str | None) -> str | None:
    if q is None:
        return None
    q = " ".join(q.split())
    return q[:MAX_QUERY] or None


def _aware(value: datetime | None) -> datetime | None:
    """A date without a time zone is read as UTC."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _filtered(stmt: Select[Any], search: CaseSearch) -> Select[Any]:
    since, until = _aware(search.since), _aware(search.until)
    if since is not None and until is not None and since > until:
        raise InvalidInput("since is after until", code=ec.DOS_BAD_SEARCH)
    q = _query_text(search.q)
    if q is not None:
        stmt = stmt.where(
            or_(
                _french_vector().op("@@")(func.websearch_to_tsquery(_FRENCH, q)),
                func.to_tsvector(_SIMPLE, func.coalesce(Claim.text_original, "")).op("@@")(
                    func.websearch_to_tsquery(_SIMPLE, q)
                ),
                # Word starts and partial words ("chôm") that full text does not match.
                _searched_text().ilike(f"%{_escape_like(q)}%", escape="\\"),
            )
        )
    if search.kind is not None:
        stmt = stmt.where(Case.kind == search.kind.value)
    if search.status is not None:
        stmt = stmt.where(Case.status == search.status.value)
    if search.veracity:
        stmt = stmt.where(Verdict.veracity.in_(search.veracity))
    if search.claim_type:
        stmt = stmt.where(Claim.claim_type.in_(search.claim_type))
    if search.context_need is not None:
        stmt = stmt.where(Verdict.context_need == search.context_need)
    if search.lang is not None:
        stmt = stmt.where(Claim.lang_original == search.lang.lower())
    if search.jurisdiction is not None:
        stmt = stmt.where(Claim.jurisdictions.contains([search.jurisdiction.upper()]))
    if search.platform is not None:
        stmt = stmt.where(ContentItem.platform == search.platform.lower())
    if since is not None:
        stmt = stmt.where(Case.created_at >= since)
    if until is not None:
        stmt = stmt.where(Case.created_at < until)
    return stmt


def _verdict_state(verdict: Verdict | None, now: datetime) -> str | None:
    if verdict is None:
        return None
    if verdict.status == VerdictStatus.ACTIVE.value:
        expired = verdict.expires_at is not None and verdict.expires_at <= now
        return "expired" if expired else "current"
    return verdict.status


def _summary(
    case: Case,
    verdict: Verdict | None,
    claim: Claim | None,
    content: ContentItem | None,
    published_at: datetime | None,
    internal: bool,
    now: datetime,
) -> CaseSummary:
    return CaseSummary(
        public_id=case.public_id,
        kind=case.kind,
        title=case.title,
        platform=content.platform if content is not None else ("x" if case.x_post_id else None),
        x_post_id=None if case.x_post_id is None else str(case.x_post_id),
        external_id=(
            content.external_id
            if content is not None
            else (None if case.x_post_id is None else str(case.x_post_id))
        ),
        url=(
            content_url(content.platform, content.external_id)
            if content is not None
            else (None if case.x_post_id is None else content_url("x", str(case.x_post_id)))
        ),
        published_url=case.published_url,
        published_at=published_at,
        created_at=case.created_at,
        updated_at=case.updated_at,
        claim_type=claim.claim_type if claim is not None else None,
        claim_lang=claim.lang_original if claim is not None else None,
        jurisdictions=list(claim.jurisdictions or []) if claim is not None else [],
        veracity=verdict.veracity if verdict is not None else None,
        context_need=verdict.context_need if verdict is not None else None,
        verdict_state=_verdict_state(verdict, now),
        status=case.status if internal else None,
        priority=case.priority if internal else None,
        assigned_to=(None if not internal or case.assigned_to is None else str(case.assigned_to)),
    )


def _row_select() -> Select[Any]:
    return select(Case, Verdict, Claim, ContentItem, _published_at().label("published_at"))


# --- search ----------------------------------------------------------------------------


async def search_cases(
    session: AsyncSession,
    actor: Actor,
    search: CaseSearch,
    *,
    page: int = 1,
    per_page: int = 20,
    sort: SearchSort = SearchSort.NEWEST,
) -> CasePage:
    """Filtered, paginated list of cases (numbered pages with the total)."""
    internal = _internal(actor)
    page = max(page, 1)
    per_page = min(max(per_page, 1), 100)
    offset = (page - 1) * per_page
    if offset >= MAX_OFFSET:
        raise InvalidInput(
            "Page too deep: narrow the filters", code=ec.DOS_BAD_SEARCH, max_offset=MAX_OFFSET
        )
    base = _filtered(_joined(select(Case.id), internal), search)
    total = int(await session.scalar(select(func.count()).select_from(base.subquery())) or 0)
    stmt = _filtered(_joined(_row_select().select_from(Case), internal), search)
    q = _query_text(search.q)
    if sort == SearchSort.RELEVANCE and q is not None:
        rank = func.ts_rank(_french_vector(), func.websearch_to_tsquery(_FRENCH, q))
        stmt = stmt.order_by(rank.desc(), Case.id.desc())
    elif sort == SearchSort.OLDEST:
        stmt = stmt.order_by(Case.id.asc())
    else:
        stmt = stmt.order_by(Case.id.desc())
    rows = (await session.execute(stmt.offset(offset).limit(per_page))).all()
    now = datetime.now(UTC)
    items = [_summary(c, v, cl, co, pub, internal, now) for c, v, cl, co, pub in rows]
    return CasePage(
        items=items,
        page=page,
        per_page=per_page,
        total=total,
        pages=math.ceil(total / per_page) if total else 0,
    )


# --- details ---------------------------------------------------------------------------


async def get_case_details(
    session: AsyncSession, actor: Actor, public_id: uuid.UUID, lang: str | None = None
) -> CaseDetails:
    """Everything about one case: post, claim, verdict and note (best language, as in
    ``get_public_verdict``), number of posts carrying the claim, status timeline."""
    internal = _internal(actor)
    stmt = _joined(_row_select().select_from(Case), internal).where(Case.public_id == public_id)
    row = (await session.execute(stmt)).first()
    if row is None:
        raise NotFound("Case not found", code=ec.DOS_NOT_FOUND, public_id=str(public_id))
    case, verdict, claim, content, published_at = row
    now = datetime.now(UTC)
    wanted = norm_lang(lang)
    summary = _summary(case, verdict, claim, content, published_at, internal, now)

    content_view = None
    if content is not None:
        content_view = {
            "platform": content.platform,
            "external_id": content.external_id,
            "url": content_url(content.platform, content.external_id),
            "lang": content.lang,
        }
    claim_view = None
    if claim is not None:
        french = wanted == "fr"
        claim_view = {
            "text": claim.text_fr if french else claim.text_original,
            "lang": "fr" if french else claim.lang_original,
            "text_fr": claim.text_fr,
            "claim_type": claim.claim_type,
            "subject": claim.subject,
            "place": claim.place,
            "period_start": claim.period_start,
            "period_end": claim.period_end,
            "jurisdictions": list(claim.jurisdictions or []),
        }
    verdict_view = None
    note = None
    posts_count = 0
    if verdict is not None:
        verdict_view = {
            "id": verdict.public_id,
            "veracity": verdict.veracity,
            "context_need": verdict.context_need,
            "context_types": list(verdict.context_types or []),
            "validated_at": verdict.validated_at,
            "expires_at": verdict.expires_at,
            "state": _verdict_state(verdict, now),
        }
        post_lang = (
            content.lang if content is not None else (claim.lang_original if claim else "fr")
        )
        note = await public_note(session, verdict, wanted, post_lang)
        posts_count = int(
            await session.scalar(
                select(func.count(func.distinct(ContentClaim.content_id)))
                .join(ClaimGroupMember, ClaimGroupMember.claim_id == ContentClaim.claim_id)
                .where(
                    ClaimGroupMember.group_id == verdict.claim_group_id,
                    ClaimGroupMember.removed_at.is_(None),
                )
            )
            or 0
        )

    events = (
        await session.scalars(
            select(CaseEvent).where(CaseEvent.case_id == case.id).order_by(CaseEvent.id)
        )
    ).all()
    timeline: list[dict[str, Any]] = []
    for ev in events:
        entry: dict[str, Any] = {"status": ev.to_status, "at": ev.created_at}
        if internal:
            entry |= {"from_status": ev.from_status, "actor": ev.actor, "reason": ev.reason}
        timeline.append(entry)

    internal_view = None
    if internal:
        internal_view = {
            "discord_thread_id": (
                None if case.discord_thread_id is None else str(case.discord_thread_id)
            ),
            "draft_id": verdict.public_id if verdict is not None else None,
            "verdict_status": verdict.status if verdict is not None else None,
            "confidence": verdict.confidence if verdict is not None else None,
            "level": None,
            "version": case.version,
            "opened_by": case.opened_by,
        }
        if verdict is not None:
            internal_view["level"] = await session.scalar(
                select(ClaimGroup.level).where(ClaimGroup.id == verdict.claim_group_id)
            )
    return CaseDetails(
        summary=summary,
        content=content_view,
        claim=claim_view,
        verdict=verdict_view,
        note=note,
        posts_count=posts_count,
        timeline=timeline,
        internal=internal_view,
    )


# --- statistics ------------------------------------------------------------------------


def _counts(rows: Any) -> dict[str, int]:
    out = {str(key): int(n) for key, n in rows if key is not None}
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


async def case_stats(
    session: AsyncSession,
    actor: Actor,
    search: CaseSearch,
    *,
    interval: StatsInterval = StatsInterval.MONTH,
) -> CaseStats:
    """Counts of the cases matching ``search``: by kind, veracity (validated verdicts),
    claim type, claim language, jurisdiction (top 20), status (internal), and a timeline
    of case creations (last 400 periods at most)."""
    internal = _internal(actor)

    async def grouped(column: Any, *, limit: int | None = None) -> dict[str, int]:
        stmt = _filtered(
            _joined(select(column, func.count()).select_from(Case), internal), search
        ).group_by(column)
        if limit is not None:
            stmt = stmt.order_by(func.count().desc(), column).limit(limit)
        return _counts((await session.execute(stmt)).all())

    total_stmt = _filtered(_joined(select(func.count()).select_from(Case), internal), search)
    total = int(await session.scalar(total_stmt) or 0)
    # The public join already keeps validated verdicts only; the team sees the same
    # figure (drafts are not verdicts).
    veracity_col = Verdict.veracity if not internal else _validated_veracity()
    period = func.date_trunc(interval.value, Case.created_at).label("period")
    timeline_stmt = (
        _filtered(_joined(select(period, func.count()).select_from(Case), internal), search)
        .group_by(period)
        .order_by(period.desc())
        .limit(MAX_BUCKETS)
    )
    timeline = [
        {"period": p.date(), "count": int(n)}
        for p, n in reversed((await session.execute(timeline_stmt)).all())
    ]
    jurisdiction = func.unnest(Claim.jurisdictions).label("jurisdiction")
    return CaseStats(
        total=total,
        by_kind=await grouped(Case.kind),
        by_veracity=await grouped(veracity_col),
        by_claim_type=await grouped(Claim.claim_type),
        by_lang=await grouped(Claim.lang_original),
        by_jurisdiction=await grouped(jurisdiction, limit=TOP_JURISDICTIONS),
        timeline=timeline,
        interval=interval.value,
        by_status=await grouped(Case.status) if internal else None,
    )


def _validated_veracity() -> Any:
    return sql_case((_visible_verdict(), Verdict.veracity), else_=None)
