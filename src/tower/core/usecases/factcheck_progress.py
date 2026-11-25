"""Fact-checks being prepared (read only): team requests still in the queue and
investigations not yet turned into a draft, with their stage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain.factcheck.urls import content_url
from tower.core.domain.factcheck.values import InvestigationStage
from tower.core.domain.security import Actor, Permission
from tower.core.factcheck import names
from tower.core.models.factcheck import Claim, ClaimGroup, Investigation
from tower.core.models.ingestion import ContentItem
from tower.core.models.jobs import Job

__all__ = ["FAILED_WINDOW", "InProgress", "PendingRequest", "RunningInvestigation", "in_progress"]

FAILED_WINDOW = timedelta(hours=24)
"""Failed investigations stay listed this long, so the team sees what broke."""

_CLAIM_CHARS = 200


@dataclass(frozen=True, slots=True)
class PendingRequest:
    """A team request still before extraction: the post is read, then its claims
    extracted (``factcheck.request`` then ``factcheck.extract``)."""

    step: str
    """``reading`` (post not read yet) or ``extracting``."""
    status: str
    """Job status: ``queued`` or ``running``."""
    level: str | None
    platform: str | None
    external_id: str | None
    url: str | None
    requested_by: str
    attempts: int
    last_error: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RunningInvestigation:
    id: str
    group_id: str
    level: str
    depth: str
    stage: str
    round: int
    cost_usd: float
    claim: str | None
    """Representative claim, in French, clipped."""
    platform: str | None
    external_id: str | None
    url: str | None
    requested_by: str
    error: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class InProgress:
    requests: list[PendingRequest]
    investigations: list[RunningInvestigation]


def _clip(text: str | None) -> str | None:
    if text is None or len(text) <= _CLAIM_CHARS:
        return text
    return text[: _CLAIM_CHARS - 1].rstrip() + "…"


async def _requests(session: AsyncSession, limit: int) -> list[PendingRequest]:
    jobs = (
        await session.scalars(
            select(Job)
            .where(
                Job.kind.in_((names.JOB_REQUEST, names.JOB_EXTRACT)),
                Job.status.in_(("queued", "running")),
                func.coalesce(Job.payload["requested_by"].astext, names.FACTCHECK_ACTOR)
                != names.FACTCHECK_ACTOR,
            )
            .order_by(Job.created_at.desc(), Job.id.desc())
            .limit(limit)
        )
    ).all()
    content_ids = [
        int(j.payload["content_id"])
        for j in jobs
        if j.kind == names.JOB_EXTRACT and str(j.payload.get("content_id", "")).isdigit()
    ]
    contents: dict[int, tuple[str, str]] = {}
    if content_ids:
        rows = await session.execute(
            select(ContentItem.id, ContentItem.platform, ContentItem.external_id).where(
                ContentItem.id.in_(content_ids)
            )
        )
        contents = {r[0]: (r[1], r[2]) for r in rows}
    out: list[PendingRequest] = []
    for job in jobs:
        payload: dict[str, Any] = job.payload
        if job.kind == names.JOB_REQUEST:
            platform, external_id = payload.get("platform"), payload.get("external_id")
        else:
            platform, external_id = contents.get(int(payload.get("content_id") or 0), (None, None))
        out.append(
            PendingRequest(
                step="reading" if job.kind == names.JOB_REQUEST else "extracting",
                status=job.status,
                level=payload.get("level"),
                platform=None if platform is None else str(platform),
                external_id=None if external_id is None else str(external_id),
                url=content_url(str(platform), str(external_id))
                if platform and external_id
                else None,
                requested_by=str(payload.get("requested_by")),
                attempts=job.attempts,
                last_error=_clip(job.last_error),
                created_at=job.created_at,
            )
        )
    return out


async def _investigations(
    session: AsyncSession, limit: int, now: datetime
) -> list[RunningInvestigation]:
    failed = InvestigationStage.FAILED.value
    rows = (
        await session.execute(
            select(Investigation, ClaimGroup.public_id, Claim.text_fr, ContentItem)
            .join(ClaimGroup, ClaimGroup.id == Investigation.claim_group_id)
            .outerjoin(Claim, Claim.id == ClaimGroup.representative_claim_id)
            .outerjoin(ContentItem, ContentItem.id == Investigation.content_id)
            .where(
                Investigation.stage != InvestigationStage.ASSEMBLED.value,
                or_(
                    Investigation.stage != failed,
                    and_(
                        Investigation.stage == failed,
                        Investigation.updated_at >= now - FAILED_WINDOW,
                    ),
                ),
            )
            .order_by(Investigation.created_at.desc(), Investigation.id.desc())
            .limit(limit)
        )
    ).all()
    return [
        RunningInvestigation(
            id=str(inv.public_id),
            group_id=str(group_id),
            level=inv.level,
            depth=inv.depth,
            stage=inv.stage,
            round=inv.round,
            cost_usd=round(inv.cost_usd_micros / 1_000_000, 4),
            claim=_clip(text_fr),
            platform=content.platform if content is not None else None,
            external_id=content.external_id if content is not None else None,
            url=content_url(content.platform, content.external_id) if content else None,
            requested_by=inv.requested_by,
            error=_clip(inv.error),
            created_at=inv.created_at,
            updated_at=inv.updated_at,
        )
        for inv, group_id, text_fr, content in rows
    ]


async def in_progress(session: AsyncSession, actor: Actor, *, limit: int = 25) -> InProgress:
    """Team requests not yet extracted (automatic flows left out) and investigations not
    yet assembled into a draft (failed ones for ``FAILED_WINDOW``), newest first."""
    actor.require(Permission.CASE_READ)
    limit = min(max(limit, 1), 100)
    now = datetime.now(UTC)
    return InProgress(
        requests=await _requests(session, limit),
        investigations=await _investigations(session, limit, now),
    )
