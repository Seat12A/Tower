"""Badge requests and badge reports (public submissions, team decisions).

Nothing about the submitter is stored for a report; a request only keeps the contact
the requester chose to give (read by the team, never public, never in events).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from tower.core.domain import error_codes as ec
from tower.core.domain.badges import (
    MAX_COMMENT,
    MAX_CONTACT,
    MAX_EVIDENCE_URLS,
    MAX_JUSTIFICATION,
    MAX_LABEL,
    MAX_NOTE,
    MAX_TITLE,
    AccountRef,
    AffiliationSource,
    AffiliationStatus,
    BadgeSource,
    BadgeStatus,
    ReportReason,
    ReportStatus,
    RequestKind,
    RequestStatus,
    check_text,
    check_url,
    parse_category,
)
from tower.core.domain.errors import Conflict, InvalidInput, NotFound
from tower.core.domain.security import Actor, Permission
from tower.core.events.outbox import publish
from tower.core.models.badges import Badge, BadgeAffiliation, BadgeReport, BadgeRequest
from tower.core.models.ingestion import PlatformAccount
from tower.core.usecases.badges import (
    account_ref_payload,
    active_org_badge,
    ensure_account,
    find_account,
    grant_badge,
    required,
    utcnow,
)


def request_payload(
    r: BadgeRequest,
    account: PlatformAccount,
    org_account: PlatformAccount | None,
    *,
    with_contact: bool,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "public_id": str(r.public_id),
        "kind": r.kind,
        "account": {**account_ref_payload(account), "handle": account.handle},
        "requested_category": r.requested_category,
        "org": account_ref_payload(org_account) if org_account is not None else None,
        "title": r.title,
        "justification": r.justification,
        "evidence_urls": list(r.evidence_urls or []),
        "status": r.status,
        "submitted_by": r.submitted_by,
        "decided_by": r.decided_by,
        "decided_at": r.decided_at.isoformat() if r.decided_at else None,
        "decision_note": r.decision_note,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }
    if with_contact:
        out["contact"] = r.contact
    return out


def report_payload(r: BadgeReport, account: PlatformAccount) -> dict[str, Any]:
    return {
        "public_id": str(r.public_id),
        "account": {**account_ref_payload(account), "handle": account.handle},
        "reason": r.reason,
        "comment": r.comment,
        "status": r.status,
        "resolved_by": r.resolved_by,
        "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
        "resolution_note": r.resolution_note,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


# --- Requests -------------------------------------------------------------------------


async def submit_request(
    session: AsyncSession,
    actor: Actor,
    *,
    kind: str,
    account: AccountRef,
    category: str | None = None,
    org: AccountRef | None = None,
    title: str | None = None,
    justification: str | None = None,
    contact: str | None = None,
    evidence_urls: Sequence[str] = (),
    handle: str | None = None,
) -> BadgeRequest:
    """Badge request (``kind=badge``, ``category`` required) or affiliation request
    (``kind=affiliation``, ``org`` must hold an affiliable badge). One pending request
    per (kind, account, organisation)."""
    actor.require(Permission.BADGES_SUBMIT)
    try:
        req_kind = RequestKind(kind)
    except ValueError:
        raise InvalidInput("Unknown request kind", code=ec.BDG_BAD_TEXT, field="kind") from None
    cat = parse_category(category) if category else None
    if req_kind is RequestKind.BADGE and cat is None:
        raise InvalidInput("category is required", code=ec.BDG_BAD_CATEGORY)
    org_badge: Badge | None = None
    if req_kind is RequestKind.AFFILIATION:
        if org is None:
            raise InvalidInput("org is required", code=ec.BDG_NOT_AFFILIABLE)
        org_badge, _ = await active_org_badge(session, org)
    if len(evidence_urls) > MAX_EVIDENCE_URLS:
        raise InvalidInput(
            "Too many evidence URLs", code=ec.BDG_BAD_TEXT, max_items=MAX_EVIDENCE_URLS
        )
    urls = [u for u in (check_url(x, field="evidence_urls") for x in evidence_urls) if u]
    acc = await ensure_account(session, account, handle=handle)
    blocked = await session.scalar(
        select(Badge.id).where(Badge.account_id == acc.id, Badge.status == "blocked")
    )
    if blocked is not None:
        raise Conflict("Account is blocked from badges", code=ec.BDG_ACCOUNT_BLOCKED)
    if org_badge is not None and org_badge.account_id == acc.id:
        raise InvalidInput(
            "An account cannot be affiliated with itself", code=ec.BDG_SELF_AFFILIATION
        )
    request = BadgeRequest(
        kind=req_kind.value,
        account_id=acc.id,
        requested_category=cat.value if cat else None,
        org_badge_id=org_badge.id if org_badge else None,
        title=check_text(title, field="title", max_chars=MAX_TITLE),
        justification=check_text(justification, field="justification", max_chars=MAX_JUSTIFICATION),
        contact=check_text(contact, field="contact", max_chars=MAX_CONTACT),
        evidence_urls=urls,
        status=RequestStatus.PENDING.value,
        submitted_by=actor.label,
    )
    try:
        async with session.begin_nested():
            session.add(request)
            await session.flush()
    except IntegrityError:
        raise Conflict(
            "A pending request already exists for this account", code=ec.BDG_REQUEST_DUPLICATE
        ) from None
    await session.refresh(request)
    await publish(
        session,
        "badge.request_submitted",
        {
            "request": str(request.public_id),
            "kind": request.kind,
            "account": account_ref_payload(acc),
            "requested_category": request.requested_category,
        },
        aggregate_type="badge_request",
        aggregate_id=str(request.public_id),
        actor=actor.label,
    )
    return request


async def list_requests(
    session: AsyncSession,
    actor: Actor,
    *,
    status: str | None = "pending",
    kind: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    actor.require(Permission.BADGES_READ)
    org_badge = aliased(Badge)
    org_account = aliased(PlatformAccount)
    stmt = (
        select(BadgeRequest, PlatformAccount, org_account)
        .join(PlatformAccount, PlatformAccount.id == BadgeRequest.account_id)
        .outerjoin(org_badge, org_badge.id == BadgeRequest.org_badge_id)
        .outerjoin(org_account, org_account.id == org_badge.account_id)
        .order_by(BadgeRequest.created_at, BadgeRequest.id)
        .limit(min(max(limit, 1), 500))
        .offset(max(offset, 0))
    )
    if status is not None:
        stmt = stmt.where(BadgeRequest.status == RequestStatus(status).value)
    if kind is not None:
        stmt = stmt.where(BadgeRequest.kind == RequestKind(kind).value)
    return [
        request_payload(r, a, o, with_contact=True)
        for r, a, o in (await session.execute(stmt)).all()
    ]


async def _pending_request(session: AsyncSession, public_id: str) -> BadgeRequest:
    try:
        pid = uuid.UUID(public_id)
    except ValueError:
        raise NotFound("Badge request not found", code=ec.BDG_REQUEST_NOT_FOUND) from None
    r = await session.scalar(
        select(BadgeRequest).where(BadgeRequest.public_id == pid).with_for_update()
    )
    if r is None:
        raise NotFound("Badge request not found", code=ec.BDG_REQUEST_NOT_FOUND)
    if r.status != RequestStatus.PENDING.value:
        raise Conflict(
            "Badge request already decided", code=ec.BDG_REQUEST_DECIDED, status=r.status
        )
    return r


async def _ref_of(session: AsyncSession, account_id: int) -> AccountRef:
    account = required(await session.get(PlatformAccount, account_id), "account")
    return AccountRef.parse(account.platform, required(account.external_id, "external id"))


async def approve_request(
    session: AsyncSession,
    actor: Actor,
    public_id: str,
    *,
    category: str | None = None,
    label: str | None = None,
    note: str | None = None,
) -> BadgeRequest:
    """Approve: grants the badge (``category`` may override the requested one) or creates
    the affiliation, active and validated by the team."""
    actor.require(Permission.BADGES_MANAGE)
    r = await _pending_request(session, public_id)
    ref = await _ref_of(session, r.account_id)
    if r.kind == RequestKind.BADGE.value:
        await grant_badge(
            session,
            actor,
            ref,
            category=category or r.requested_category or "",
            label=check_text(label, field="label", max_chars=MAX_LABEL),
            source=BadgeSource.REQUEST,
        )
    else:
        from tower.core.usecases.badge_affiliations import create_affiliation

        org = required(
            await session.get(Badge, required(r.org_badge_id, "org badge id")), "org badge"
        )
        org_ref = await _ref_of(session, org.account_id)
        await create_affiliation(
            session,
            actor,
            ref,
            org_ref=org_ref,
            title=r.title,
            validate=True,
            source=AffiliationSource.REQUEST,
        )
    await _decide(session, actor, r, RequestStatus.APPROVED, note)
    return r


async def reject_request(
    session: AsyncSession, actor: Actor, public_id: str, *, note: str | None = None
) -> BadgeRequest:
    actor.require(Permission.BADGES_MANAGE)
    r = await _pending_request(session, public_id)
    await _decide(session, actor, r, RequestStatus.REJECTED, note)
    return r


async def _decide(
    session: AsyncSession,
    actor: Actor,
    r: BadgeRequest,
    status: RequestStatus,
    note: str | None,
) -> None:
    r.status = status.value
    r.decided_by = actor.label
    r.decided_at = utcnow()
    r.decision_note = check_text(note, field="note", max_chars=MAX_NOTE)
    await session.flush()
    await publish(
        session,
        "badge.request_decided",
        {"request": str(r.public_id), "kind": r.kind, "status": r.status},
        aggregate_type="badge_request",
        aggregate_id=str(r.public_id),
        actor=actor.label,
    )


# --- Reports --------------------------------------------------------------------------


async def submit_report(
    session: AsyncSession,
    actor: Actor,
    *,
    account: AccountRef,
    reason: str,
    comment: str | None = None,
) -> BadgeReport:
    """Report a displayed badge or affiliation. Only accounts that show something can be
    reported (no rows are created for unknown ids)."""
    actor.require(Permission.BADGES_SUBMIT)
    try:
        why = ReportReason(reason)
    except ValueError:
        raise InvalidInput("Unknown report reason", code=ec.BDG_BAD_TEXT, field="reason") from None
    comment = check_text(comment, field="comment", max_chars=MAX_COMMENT)
    acc = await find_account(session, account)
    shows = False
    if acc is not None:
        shows = (
            await session.scalar(
                select(Badge.id).where(
                    Badge.account_id == acc.id, Badge.status == BadgeStatus.ACTIVE.value
                )
            )
            is not None
            or await session.scalar(
                select(BadgeAffiliation.id).where(
                    BadgeAffiliation.member_account_id == acc.id,
                    BadgeAffiliation.status == AffiliationStatus.ACTIVE.value,
                )
            )
            is not None
        )
    if acc is None or not shows:
        raise NotFound(
            "No badge for this account",
            code=ec.BDG_NOT_FOUND,
            platform=account.platform.value,
            external_id=account.external_id,
        )
    report = BadgeReport(account_id=acc.id, reason=why.value, comment=comment)
    session.add(report)
    await session.flush()
    await session.refresh(report)
    await publish(
        session,
        "badge.report_submitted",
        {"report": str(report.public_id), "account": account_ref_payload(acc), "reason": why.value},
        aggregate_type="badge_report",
        aggregate_id=str(report.public_id),
        actor=actor.label,
    )
    return report


async def list_reports(
    session: AsyncSession,
    actor: Actor,
    *,
    status: str | None = "open",
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    actor.require(Permission.BADGES_READ)
    stmt = (
        select(BadgeReport, PlatformAccount)
        .join(PlatformAccount, PlatformAccount.id == BadgeReport.account_id)
        .order_by(BadgeReport.created_at, BadgeReport.id)
        .limit(min(max(limit, 1), 500))
        .offset(max(offset, 0))
    )
    if status is not None:
        stmt = stmt.where(BadgeReport.status == ReportStatus(status).value)
    return [report_payload(r, a) for r, a in (await session.execute(stmt)).all()]


async def resolve_report(
    session: AsyncSession,
    actor: Actor,
    public_id: str,
    *,
    status: str,
    note: str | None = None,
) -> BadgeReport:
    """``open`` → ``resolved`` (action taken) or ``dismissed``."""
    actor.require(Permission.BADGES_MANAGE)
    try:
        target = ReportStatus(status)
        pid = uuid.UUID(public_id)
    except ValueError:
        raise NotFound("Badge report not found", code=ec.BDG_REPORT_NOT_FOUND) from None
    if target is ReportStatus.OPEN:
        raise InvalidInput("status must be resolved or dismissed", code=ec.BDG_BAD_TEXT)
    r = await session.scalar(
        select(BadgeReport).where(BadgeReport.public_id == pid).with_for_update()
    )
    if r is None:
        raise NotFound("Badge report not found", code=ec.BDG_REPORT_NOT_FOUND)
    if r.status != ReportStatus.OPEN.value:
        raise Conflict("Badge report already handled", code=ec.BDG_REPORT_HANDLED, status=r.status)
    r.status = target.value
    r.resolved_by = actor.label
    r.resolved_at = utcnow()
    r.resolution_note = check_text(note, field="note", max_chars=MAX_NOTE)
    await session.flush()
    await publish(
        session,
        "badge.report_resolved",
        {"report": str(r.public_id), "status": r.status},
        aggregate_type="badge_report",
        aggregate_id=str(r.public_id),
        actor=actor.label,
    )
    return r


async def request_parts(
    session: AsyncSession, r: BadgeRequest
) -> tuple[PlatformAccount, PlatformAccount | None]:
    account = required(await session.get(PlatformAccount, r.account_id), "account")
    org_account = None
    if r.org_badge_id is not None:
        org = await session.get(Badge, r.org_badge_id)
        if org is not None:
            org_account = await session.get(PlatformAccount, org.account_id)
    return account, org_account


async def report_with_account(session: AsyncSession, r: BadgeReport) -> dict[str, Any]:
    account = required(await session.get(PlatformAccount, r.account_id), "account")
    return report_payload(r, account)
