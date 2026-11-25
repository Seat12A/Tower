"""Read endpoints of cases for the website: search, statistics and full details.

Declared before ``cases.router`` so that ``/v1/cases/search`` and ``/v1/cases/stats``
are not taken for a case id. Not mirrored under the old ``/v1/dossiers`` paths.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Response

from tower.core.domain.cases import CaseKind, CaseStatus
from tower.core.domain.factcheck.values import ClaimType, ContextNeed, Veracity
from tower.core.domain.platforms import Platform
from tower.core.domain.security import Actor, Permission
from tower.core.usecases import case_views as uc
from tower.gate.deps import ActorDep, ReadSession
from tower.gate.schemas import CaseDetailsOut, CaseId, CasePageOut, CaseStatsOut, CaseSummaryOut

router = APIRouter(prefix="/v1/cases", tags=["cases"])

PUBLIC_CACHE_CONTROL = "public, max-age=300"

Q = Annotated[
    str | None,
    Query(max_length=uc.MAX_QUERY, description="Text searched in the title and the claim"),
]
Kind = Annotated[CaseKind | None, Query()]
Status = Annotated[CaseStatus | None, Query(description="Team only (public: published)")]
Veracities = Annotated[list[Veracity] | None, Query(description="Repeatable (OR)")]
ClaimTypes = Annotated[list[ClaimType] | None, Query(description="Repeatable (OR)")]
Need = Annotated[ContextNeed | None, Query()]
Lang = Annotated[
    str | None, Query(pattern=r"^[A-Za-z]{2,3}$", description="Original claim language")
]
Jurisdiction = Annotated[
    str | None, Query(pattern=r"^[A-Za-z]{2,3}$", description="ISO 3166 code, EU or INT")
]
PlatformQ = Annotated[Platform | None, Query()]
Since = Annotated[datetime | None, Query(description="Created at or after (ISO 8601)")]
Until = Annotated[datetime | None, Query(description="Created before (ISO 8601)")]


def _cache_public(response: Response, actor: Actor) -> None:
    """Anonymous answers are the same for everyone: let the site and a CDN cache them."""
    if not actor.can(Permission.CASE_READ):
        response.headers["Cache-Control"] = PUBLIC_CACHE_CONTROL


def _search(
    q: str | None,
    kind: CaseKind | None,
    status: CaseStatus | None,
    veracity: list[Veracity] | None,
    claim_type: list[ClaimType] | None,
    context_need: ContextNeed | None,
    lang: str | None,
    jurisdiction: str | None,
    platform: Platform | None,
    since: datetime | None,
    until: datetime | None,
) -> uc.CaseSearch:
    return uc.CaseSearch(
        q=q,
        kind=kind,
        status=status,
        veracity=tuple(v.value for v in veracity or ()),
        claim_type=tuple(c.value for c in claim_type or ()),
        context_need=None if context_need is None else context_need.value,
        lang=lang,
        jurisdiction=jurisdiction,
        platform=None if platform is None else platform.value,
        since=since,
        until=until,
    )


@router.get("/search", response_model=CasePageOut)
async def search_cases(
    response: Response,
    actor: ActorDep,
    session: ReadSession,
    q: Q = None,
    kind: Kind = None,
    status: Status = None,
    veracity: Veracities = None,
    claim_type: ClaimTypes = None,
    context_need: Need = None,
    lang: Lang = None,
    jurisdiction: Jurisdiction = None,
    platform: PlatformQ = None,
    since: Since = None,
    until: Until = None,
    sort: uc.SearchSort = uc.SearchSort.NEWEST,
    page: Annotated[int, Query(ge=1)] = 1,
    per_page: Annotated[int, Query(ge=1, le=100)] = 20,
) -> CasePageOut:
    """Search and filter cases, numbered pages with the total (public: published only)."""
    search = _search(
        q, kind, status, veracity, claim_type, context_need, lang, jurisdiction, platform,
        since, until,
    )  # fmt: skip
    result = await uc.search_cases(session, actor, search, page=page, per_page=per_page, sort=sort)
    _cache_public(response, actor)
    return CasePageOut(
        items=[CaseSummaryOut.model_validate(item, from_attributes=True) for item in result.items],
        page=result.page,
        per_page=result.per_page,
        total=result.total,
        pages=result.pages,
    )


@router.get("/stats", response_model=CaseStatsOut)
async def case_stats(
    response: Response,
    actor: ActorDep,
    session: ReadSession,
    q: Q = None,
    kind: Kind = None,
    status: Status = None,
    veracity: Veracities = None,
    claim_type: ClaimTypes = None,
    context_need: Need = None,
    lang: Lang = None,
    jurisdiction: Jurisdiction = None,
    platform: PlatformQ = None,
    since: Since = None,
    until: Until = None,
    interval: uc.StatsInterval = uc.StatsInterval.MONTH,
) -> CaseStatsOut:
    """Counts of the cases matching the same filters as the search."""
    search = _search(
        q, kind, status, veracity, claim_type, context_need, lang, jurisdiction, platform,
        since, until,
    )  # fmt: skip
    stats = await uc.case_stats(session, actor, search, interval=interval)
    _cache_public(response, actor)
    return CaseStatsOut.model_validate(stats, from_attributes=True)


@router.get("/{case_id}/details", response_model=CaseDetailsOut)
async def case_details(
    case_id: CaseId,
    response: Response,
    actor: ActorDep,
    session: ReadSession,
    lang: Annotated[str | None, Query(max_length=16, description="Note language")] = None,
) -> CaseDetailsOut:
    """Everything about a case (public: published cases, validated verdicts)."""
    details = await uc.get_case_details(session, actor, case_id, lang)
    _cache_public(response, actor)
    return CaseDetailsOut.of(details)
