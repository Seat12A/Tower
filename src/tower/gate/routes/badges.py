"""Seat 12A verification badges: public lookup, submissions, team management.

Organisation endpoints (``X-Organization-Token``) are in ``routes/org.py``.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Any, Literal

import orjson
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from tower.core.domain import error_codes as ec
from tower.core.domain.badges import (
    CATEGORY_COLORS,
    CATEGORY_PRIORITY,
    MAX_EVIDENCE_URLS,
    MAX_LOOKUP_IDS,
    AccountRef,
    BadgeCategory,
    ReportReason,
)
from tower.core.domain.errors import InvalidInput
from tower.core.usecases import badge_affiliations as aff
from tower.core.usecases import badge_sources as sources
from tower.core.usecases import badge_submissions as sub
from tower.core.usecases import badges as uc
from tower.gate.deps import ActorDep, ReadSession, WriteSession
from tower.gate.ratelimit import client_ip

router = APIRouter(prefix="/v1/badges", tags=["badges"])

Platform = Annotated[str, Field(max_length=32)]
ExternalId = Annotated[str, Field(max_length=512)]


def submit_rate_limit(request: Request) -> None:
    """Per-IP token bucket of public submissions (requests, reports, X hints)."""
    if not request.app.state.badge_limiter.allow(client_ip(request)):
        raise InvalidInput("Too many badge submissions", code=ec.BDG_RATE_LIMITED)


SubmitLimit = Depends(submit_rate_limit)


class AccountIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: Platform = "x"
    external_id: ExternalId
    handle: str | None = Field(default=None, max_length=100)
    """Informative only: accounts are always identified by their native id."""

    def ref(self) -> AccountRef:
        return AccountRef.parse(self.platform, self.external_id)


# --- Public ------------------------------------------------------------------------------


class CategoryOut(BaseModel):
    category: BadgeCategory
    color: str
    rank: int
    """1 = highest priority."""


@router.get("/categories", response_model=list[CategoryOut])
async def categories(response: Response) -> list[CategoryOut]:
    """Categories, colours and priority order (static: cache it)."""
    response.headers["Cache-Control"] = "public, max-age=86400"
    return [
        CategoryOut(category=c, color=CATEGORY_COLORS[c], rank=i + 1)
        for i, c in enumerate(CATEGORY_PRIORITY)
    ]


class LookupBadge(BaseModel):
    category: BadgeCategory
    color: str
    label: str | None
    expertise: str | None
    """Field of an expert badge (French free text), else None."""


class LookupOrg(BaseModel):
    platform: str
    external_id: str
    category: BadgeCategory
    color: str
    label: str | None
    logo_url: str | None


class LookupAffiliation(BaseModel):
    org: LookupOrg
    title: str | None


class LookupEntry(BaseModel):
    category: BadgeCategory | None
    """Check mark to display: the account's own badge only (None: no check mark)."""
    color: str | None
    badge: LookupBadge | None
    affiliation: LookupAffiliation | None
    """Primary affiliation: its organisation logo goes next to the handle."""
    affiliations: list[LookupAffiliation]
    """Active affiliations, primary first (at most 5)."""


class LookupOut(BaseModel):
    platform: str
    accounts: dict[str, LookupEntry]
    """Native id → badge. An id absent from the map has no Seat 12A badge."""


@router.get("/lookup", response_model=LookupOut)
async def lookup(
    request: Request,
    response: Response,
    actor: ActorDep,
    session: ReadSession,
    ids: Annotated[str, Query(max_length=MAX_LOOKUP_IDS * 21)],
    platform: Annotated[str, Query(max_length=32)] = "x",
) -> Response:
    """Batch lookup for the extension: ``ids`` = up to 100 comma-separated native ids.

    Cacheable: ``Cache-Control: public, max-age=…`` and a strong ``ETag`` (send it back in
    ``If-None-Match`` to get a 304). Sort the ids for better shared-cache hits.
    """
    found = await uc.lookup_badges(session, actor, platform, ids.split(","))
    out = LookupOut(
        platform=platform, accounts={k: LookupEntry.model_validate(v) for k, v in found.items()}
    )
    body = orjson.dumps(out.model_dump(mode="json"), option=orjson.OPT_SORT_KEYS)
    etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    max_age = request.app.state.settings.badges_lookup_max_age_s
    headers = {"Cache-Control": f"public, max-age={max_age}", "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


@router.get("/accounts/{platform}/{external_id}")
async def account_details(
    platform: str, external_id: str, actor: ActorDep, session: ReadSession, response: Response
) -> dict[str, Any]:
    """Public details of one account: badge and all active affiliations."""
    response.headers["Cache-Control"] = "public, max-age=300"
    return await uc.account_badge_details(session, actor, uc.parse_ref(platform, external_id))


class RequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["badge", "affiliation"]
    account: AccountIn
    category: BadgeCategory | None = None
    """Required for a badge request."""
    org: AccountIn | None = None
    """Required for an affiliation request: the badged organisation."""
    title: str | None = Field(default=None, max_length=120)
    justification: str | None = Field(default=None, max_length=4000)
    contact: str | None = Field(default=None, max_length=320)
    evidence_urls: list[Annotated[str, Field(max_length=2048)]] = Field(
        default_factory=list, max_length=MAX_EVIDENCE_URLS
    )


class SubmittedOut(BaseModel):
    public_id: str
    status: str


@router.post("/requests", response_model=SubmittedOut, status_code=201, dependencies=[SubmitLimit])
async def submit_request(body: RequestIn, actor: ActorDep, session: WriteSession) -> SubmittedOut:
    """Badge or affiliation request (website, organisation). Rate-limited per IP."""
    r = await sub.submit_request(
        session,
        actor,
        kind=body.kind,
        account=body.account.ref(),
        category=body.category.value if body.category else None,
        org=body.org.ref() if body.org else None,
        title=body.title,
        justification=body.justification,
        contact=body.contact,
        evidence_urls=body.evidence_urls,
        handle=body.account.handle,
    )
    return SubmittedOut(public_id=str(r.public_id), status=r.status)


class ReportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account: AccountIn
    reason: ReportReason
    comment: str | None = Field(default=None, max_length=2000)


@router.post("/reports", response_model=SubmittedOut, status_code=201, dependencies=[SubmitLimit])
async def submit_report(body: ReportIn, actor: ActorDep, session: WriteSession) -> SubmittedOut:
    """Report a wrong badge or affiliation. Nothing about the reporter is stored."""
    r = await sub.submit_report(
        session, actor, account=body.account.ref(), reason=body.reason.value, comment=body.comment
    )
    return SubmittedOut(public_id=str(r.public_id), status=r.status)


class HintsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[Annotated[str, Field(max_length=19)]] = Field(min_length=1, max_length=50)


class HintsOut(BaseModel):
    queued: int


@router.post(
    "/x-government-hints", response_model=HintsOut, status_code=202, dependencies=[SubmitLimit]
)
async def x_government_hints(
    request: Request, body: HintsIn, actor: ActorDep, session: WriteSession
) -> HintsOut:
    """X ids the extension saw with a government check mark. Checked with the X API
    before any badge (hints are never trusted)."""
    queued = await sources.hint_x_accounts(
        session,
        actor,
        body.ids,
        recheck_days=request.app.state.settings.badges_x_recheck_days,
    )
    return HintsOut(queued=queued)


# --- Team: X handle → native id ----------------------------------------------------------


def x_lookup_client(request: Request) -> Any:
    """Process-level X client of gate, built on first use (httpx loaded lazily); None
    without ``TOWER_X_BEARER_TOKEN``. Tests set ``app.state.x_lookup``."""
    state = request.app.state
    if getattr(state, "x_lookup", None) is None:
        settings = state.settings
        if settings.x_bearer_token is None:
            return None
        from tower.core.adapters.x_client import XPlatformClient

        state.x_lookup = XPlatformClient(
            settings.x_bearer_token.get_secret_value(),
            api_base=settings.x_api_base,
            post_read_usd_micros=round(settings.x_price_post_read_usd * 1e6),
            user_read_usd_micros=round(settings.x_price_user_read_usd * 1e6),
        )
    return state.x_lookup


@router.get("/resolve/x/{handle}")
async def resolve_x_handle(request: Request, handle: str, actor: ActorDep) -> dict[str, Any]:
    """Native id of an X handle, read with the official X API (``badges:manage``; one
    billable user read on ``provider:x``). The account is then known by its id only."""
    return await sources.resolve_x_handle(
        request.app.state.db,
        actor,
        x_lookup_client(request),
        handle,
        ttl_s=request.app.state.settings.budget_reservation_ttl_s,
    )


# --- Team: badges ------------------------------------------------------------------------


class BadgeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: BadgeCategory
    label: str | None = Field(default=None, max_length=120)
    logo_url: str | None = Field(default=None, max_length=2048)
    expertise: str | None = Field(default=None, max_length=120)
    """Field of an expert badge (``TWR-BDG-027`` on another category); "" clears it."""
    note: str | None = Field(default=None, max_length=2000)
    handle: str | None = Field(default=None, max_length=100)
    version: int | None = None


class ReasonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, max_length=2000)


@router.get("")
async def list_badges(
    actor: ActorDep,
    session: ReadSession,
    status: Literal["active", "revoked", "blocked"] | None = "active",
    category: BadgeCategory | None = None,
    source: Literal["manual", "request", "wikidata", "x_government"] | None = None,
    platform: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    rows = await uc.list_badges(
        session,
        actor,
        status=status,
        category=category.value if category else None,
        source=source,
        platform=platform,
        limit=limit,
        offset=offset,
    )
    return [uc.badge_payload(b, a) for b, a in rows]


@router.get("/accounts/{platform}/{external_id}/badge")
async def get_badge(
    platform: str, external_id: str, actor: ActorDep, session: ReadSession
) -> dict[str, Any]:
    """Team view of the badge row (any status, internal fields)."""
    b, a = await uc.get_badge(session, actor, uc.parse_ref(platform, external_id))
    return uc.badge_payload(b, a)


@router.put("/accounts/{platform}/{external_id}")
async def grant_badge(
    platform: str, external_id: str, body: BadgeIn, actor: ActorDep, session: WriteSession
) -> dict[str, Any]:
    """Grant or update a badge (manual). Refused on a blocked account (``TWR-BDG-004``)."""
    b, a = await uc.grant_badge(
        session,
        actor,
        uc.parse_ref(platform, external_id),
        category=body.category.value,
        label=body.label,
        logo_url=body.logo_url,
        expertise=body.expertise,
        note=body.note,
        handle=body.handle,
        version=body.version,
    )
    return uc.badge_payload(b, a)


@router.post("/accounts/{platform}/{external_id}/revoke")
async def revoke_badge(
    platform: str, external_id: str, body: ReasonIn, actor: ActorDep, session: WriteSession
) -> dict[str, Any]:
    b, a = await uc.revoke_badge(
        session, actor, uc.parse_ref(platform, external_id), reason=body.reason
    )
    return uc.badge_payload(b, a)


@router.post("/accounts/{platform}/{external_id}/block")
async def block_account(
    platform: str, external_id: str, body: ReasonIn, actor: ActorDep, session: WriteSession
) -> dict[str, Any]:
    """Block from badges (even if Wikidata or X would give one)."""
    b, a = await uc.block_account(
        session, actor, uc.parse_ref(platform, external_id), reason=body.reason
    )
    return uc.badge_payload(b, a)


@router.post("/accounts/{platform}/{external_id}/unblock")
async def unblock_account(
    platform: str, external_id: str, actor: ActorDep, session: WriteSession
) -> dict[str, Any]:
    b, a = await uc.unblock_account(session, actor, uc.parse_ref(platform, external_id))
    return uc.badge_payload(b, a)


@router.get("/accounts/{platform}/{external_id}/history")
async def history(
    platform: str,
    external_id: str,
    actor: ActorDep,
    session: ReadSession,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict[str, Any]]:
    """Grants, revocations, blocks and affiliations of the account, with who did them."""
    return await uc.badge_history(session, actor, uc.parse_ref(platform, external_id), limit=limit)


# --- Team: organisation keys ----------------------------------------------------------


class OrgKeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(default=None, max_length=120)


@router.post("/accounts/{platform}/{external_id}/org-keys", status_code=201)
async def create_org_key(
    platform: str, external_id: str, body: OrgKeyIn, actor: ActorDep, session: WriteSession
) -> dict[str, Any]:
    """New organisation key. The token is shown **once**: give it to the organisation."""
    key, token = await uc.create_org_key(
        session, actor, uc.parse_ref(platform, external_id), label=body.label
    )
    return {"public_id": str(key.public_id), "label": key.label, "token": token}


@router.get("/accounts/{platform}/{external_id}/org-keys")
async def list_org_keys(
    platform: str, external_id: str, actor: ActorDep, session: ReadSession
) -> list[dict[str, Any]]:
    keys = await uc.list_org_keys(session, actor, uc.parse_ref(platform, external_id))
    return [
        {
            "public_id": str(k.public_id),
            "label": k.label,
            "created_by": k.created_by,
            "created_at": k.created_at.isoformat(),
            "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None,
        }
        for k in keys
    ]


@router.post("/org-keys/{public_id}/revoke", status_code=204)
async def revoke_org_key(public_id: str, actor: ActorDep, session: WriteSession) -> Response:
    await uc.revoke_org_key(session, actor, public_id)
    return Response(status_code=204)


# --- Affiliations (team, or the organisation for its own) ------------------------------


class AffiliationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    member: AccountIn
    org: AccountIn
    title: str | None = Field(default=None, max_length=120)
    validate_now: bool = True
    """Team: active at once (validated by the team); false → pending."""


@router.get("/affiliations")
async def list_affiliations(
    actor: ActorDep,
    session: ReadSession,
    status: Literal["pending", "active", "rejected", "revoked", "expired"] | None = None,
    org_platform: str = "x",
    org_id: str | None = None,
    member_platform: str = "x",
    member_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    return await aff.list_affiliations(
        session,
        actor,
        status=status,
        org_ref=uc.parse_ref(org_platform, org_id) if org_id else None,
        member_ref=uc.parse_ref(member_platform, member_id) if member_id else None,
        limit=limit,
        offset=offset,
    )


@router.post("/affiliations", status_code=201)
async def create_affiliation(
    body: AffiliationIn, actor: ActorDep, session: WriteSession
) -> dict[str, Any]:
    a = await aff.create_affiliation(
        session,
        actor,
        body.member.ref(),
        org_ref=body.org.ref(),
        title=body.title,
        validate=body.validate_now,
        handle=body.member.handle,
    )
    return await aff.payload_of(session, a)


@router.get("/affiliations/{public_id}")
async def get_affiliation(public_id: str, actor: ActorDep, session: ReadSession) -> dict[str, Any]:
    return await aff.get_affiliation(session, actor, public_id)


@router.post("/affiliations/{public_id}/validate")
async def validate_affiliation(
    public_id: str, actor: ActorDep, session: WriteSession
) -> dict[str, Any]:
    """``pending`` or ``expired`` → ``active`` for one year (team or the organisation)."""
    a = await aff.validate_affiliation(session, actor, public_id)
    return await aff.payload_of(session, a)


@router.post("/affiliations/{public_id}/reconfirm")
async def reconfirm_affiliation(
    public_id: str, actor: ActorDep, session: WriteSession
) -> dict[str, Any]:
    """Renew an active affiliation for one more year (team or the organisation)."""
    a = await aff.reconfirm_affiliation(session, actor, public_id)
    return await aff.payload_of(session, a)


@router.post("/affiliations/{public_id}/reject")
async def reject_affiliation(
    public_id: str, body: ReasonIn, actor: ActorDep, session: WriteSession
) -> dict[str, Any]:
    a = await aff.reject_affiliation(session, actor, public_id, reason=body.reason)
    return await aff.payload_of(session, a)


@router.post("/affiliations/{public_id}/revoke")
async def revoke_affiliation(
    public_id: str, body: ReasonIn, actor: ActorDep, session: WriteSession
) -> dict[str, Any]:
    a = await aff.revoke_affiliation(session, actor, public_id, reason=body.reason)
    return await aff.payload_of(session, a)


@router.post("/affiliations/{public_id}/primary")
async def primary_affiliation(
    public_id: str, actor: ActorDep, session: WriteSession
) -> dict[str, Any]:
    """Make this active affiliation the displayed one (team)."""
    a = await aff.set_primary_affiliation(session, actor, public_id)
    return await aff.payload_of(session, a)


# --- Team: requests, reports, imports -------------------------------------------------


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: BadgeCategory | None = None
    """Approval of a badge request: overrides the requested category."""
    label: str | None = Field(default=None, max_length=120)
    expertise: str | None = Field(default=None, max_length=120)
    """Approval as an expert badge: field of expertise."""
    note: str | None = Field(default=None, max_length=2000)


@router.get("/requests")
async def list_requests(
    actor: ActorDep,
    session: ReadSession,
    status: Literal["pending", "approved", "rejected"] | None = "pending",
    kind: Literal["badge", "affiliation"] | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    return await sub.list_requests(
        session, actor, status=status, kind=kind, limit=limit, offset=offset
    )


@router.get("/requests/{public_id}")
async def get_request(public_id: str, actor: ActorDep, session: ReadSession) -> dict[str, Any]:
    return await sub.get_request(session, actor, public_id)


@router.post("/requests/{public_id}/approve")
async def approve_request(
    public_id: str, body: DecisionIn, actor: ActorDep, session: WriteSession
) -> dict[str, Any]:
    r = await sub.approve_request(
        session,
        actor,
        public_id,
        category=body.category.value if body.category else None,
        label=body.label,
        expertise=body.expertise,
        note=body.note,
    )
    account, org = await sub.request_parts(session, r)
    return sub.request_payload(r, account, org, with_contact=False)


@router.post("/requests/{public_id}/reject")
async def reject_request(
    public_id: str, body: DecisionIn, actor: ActorDep, session: WriteSession
) -> dict[str, Any]:
    r = await sub.reject_request(session, actor, public_id, note=body.note)
    account, org = await sub.request_parts(session, r)
    return sub.request_payload(r, account, org, with_contact=False)


class ResolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["resolved", "dismissed"]
    note: str | None = Field(default=None, max_length=2000)


@router.get("/reports")
async def list_reports(
    actor: ActorDep,
    session: ReadSession,
    status: Literal["open", "resolved", "dismissed"] | None = "open",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    return await sub.list_reports(session, actor, status=status, limit=limit, offset=offset)


@router.get("/reports/{public_id}")
async def get_report(public_id: str, actor: ActorDep, session: ReadSession) -> dict[str, Any]:
    return await sub.get_report(session, actor, public_id)


@router.post("/reports/{public_id}/resolve")
async def resolve_report(
    public_id: str, body: ResolveIn, actor: ActorDep, session: WriteSession
) -> dict[str, Any]:
    r = await sub.resolve_report(session, actor, public_id, status=body.status, note=body.note)
    return await sub.report_with_account(session, r)


class ImportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scopes: list[Annotated[str, Field(max_length=16)]] | None = Field(default=None, max_length=50)
    """Country QIDs or ``intl``; default: ``TOWER_BADGES_WIKIDATA_SCOPES``."""


@router.post("/imports/wikidata", status_code=202)
async def import_wikidata(
    request: Request, body: ImportIn, actor: ActorDep, session: WriteSession
) -> dict[str, Any]:
    """Start a Wikidata import now (needs a hangar running the ``badges`` service)."""
    scopes = body.scopes or request.app.state.settings.badges_wikidata_scope_list
    return {"scopes": await sources.request_wikidata_import(session, actor, scopes)}


@router.get("/imports")
async def imports(actor: ActorDep, session: ReadSession) -> list[dict[str, Any]]:
    return await sources.import_runs(session, actor)
