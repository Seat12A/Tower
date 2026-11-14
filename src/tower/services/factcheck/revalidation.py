"""Expiry and revalidation of verdicts: periodic task ``factcheck:expire`` and job
``factcheck.revalidate`` (queue fc-bulk).

``expire`` marks the verdicts past their ``expires_at`` as ``expired`` and enqueues one
revalidation per verdict. The job brings the group back to a verdict: relink to a newer
validated fact of the series, inherit through an active band, or investigate again.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.config import Settings
from tower.core.domain import error_codes as ec
from tower.core.domain.factcheck.reuse import ReuseContext, can_reuse
from tower.core.domain.factcheck.values import (
    BandStatus,
    ClaimGroupState,
    ClaimType,
    FactStatus,
    GroupEventKind,
    InvestigationStage,
    ReuseRule,
    VerdictStatus,
)
from tower.core.domain.security import Actor
from tower.core.errors import capture
from tower.core.factcheck import names
from tower.core.factcheck.profiles import active_profile
from tower.core.jobs.queue import PermanentError, RetryLater
from tower.core.logging import get_logger
from tower.core.models.factcheck import (
    Claim,
    ClaimGroup,
    ContentClaim,
    Investigation,
    Verdict,
)
from tower.core.models.factcheck_v1 import ClaimGroupEvent, ReferenceFact, VerdictBand
from tower.core.models.ingestion import ContentItem
from tower.core.services.base import JobContext
from tower.core.usecases import claims as claims_uc
from tower.core.usecases import expiry as expiry_uc
from tower.core.usecases import references as references_uc
from tower.core.usecases.reuse import inherit_band_verdict

__all__ = ["expire", "revalidate"]

log = get_logger(__name__)

_WAITING = (ClaimGroupState.TO_REVALIDATE.value, ClaimGroupState.QUEUED.value)
_FINISHED = (InvestigationStage.FAILED.value,)
_RETRY_S = 300.0


def _actor() -> Actor:
    return Actor.system(names.FACTCHECK_ACTOR)


async def expire(session: AsyncSession, settings: Settings) -> int:
    """Periodic ``factcheck:expire``: expired verdicts and provisional notes."""
    del settings
    now = datetime.now(UTC)
    actor = _actor()
    count = await expiry_uc.expire_verdicts(session, actor, now=now)
    count += await expiry_uc.expire_provisional_notes(session, actor, now=now)
    return count


async def _has_running_investigation(
    session: AsyncSession, group: ClaimGroup, verdict: Verdict
) -> bool:
    """An investigation that is not failed and is either still in progress or newer than
    the expired verdict's validation (its draft may be waiting for review)."""
    rows = await session.scalars(
        select(Investigation).where(
            Investigation.claim_group_id == group.id,
            Investigation.stage.not_in(_FINISHED),
        )
    )
    cutoff = verdict.validated_at or verdict.created_at
    for inv in rows:
        if inv.stage not in (
            InvestigationStage.COMPLETED.value,
            InvestigationStage.ASSEMBLED.value,
        ):
            return True
        if inv.created_at > cutoff:
            return True
    return False


async def _newer_fact(session: AsyncSession, fact: ReferenceFact) -> ReferenceFact | None:
    return (
        await session.execute(
            select(ReferenceFact)
            .where(
                ReferenceFact.indicator == fact.indicator,
                ReferenceFact.territory == fact.territory,
                ReferenceFact.definition_variant.is_not_distinct_from(fact.definition_variant),
                ReferenceFact.status == FactStatus.VALIDATED.value,
                ReferenceFact.period_end > fact.period_end,
            )
            .order_by(ReferenceFact.period_end.desc(), ReferenceFact.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _latest_content(session: AsyncSession, claim_id: int) -> ContentItem | None:
    return (
        await session.execute(
            select(ContentItem)
            .join(ContentClaim, ContentClaim.content_id == ContentItem.id)
            .where(ContentClaim.claim_id == claim_id)
            .order_by(ContentClaim.created_at.desc(), ContentItem.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _try_band(
    session: AsyncSession,
    settings: Settings,
    actor: Actor,
    group: ClaimGroup,
    fact: ReferenceFact,
    claim: Claim,
    content: ContentItem | None,
    now: datetime,
) -> bool:
    """Inherit the verdict of an active band covering the group. True if inherited."""
    if group.level == "N4" or fact.status != FactStatus.VALIDATED.value:
        return False
    if group.band is None or group.band_side is None:
        return False
    band = (
        await session.execute(
            select(VerdictBand).where(
                VerdictBand.reference_fact_id == fact.id,
                VerdictBand.band == group.band,
                VerdictBand.side == group.band_side,
                VerdictBand.status == BandStatus.ACTIVE.value,
            )
        )
    ).scalar_one_or_none()
    if band is None:
        return False
    source = await session.get(Verdict, band.source_verdict_id) if band.source_verdict_id else None
    profile = await active_profile(session, settings)
    ctx = ReuseContext(
        level=group.level,
        direct_reuse_allowed=profile.level(group.level).direct_reuse,
        verdict_expires_at=source.expires_at if source is not None else None,
        verdict_status=source.status if source is not None else VerdictStatus.ACTIVE.value,
        note_regime=None,
        note_expires_at=None,
        now=now,
    )
    if not can_reuse(ctx, ReuseRule.BAND):
        return False
    await inherit_band_verdict(
        session,
        actor,
        group=group,
        band=band,
        fact=fact,
        claim=claim,
        content_id=content.id if content is not None else None,
        now=now,
    )
    return True


async def revalidate(ctx: JobContext) -> None:
    """Job ``factcheck.revalidate`` ``{"verdict_id": int}``. Idempotent: does nothing when
    the group is no longer waiting, has an active verdict or a running investigation."""
    verdict_id = int(ctx.job.payload["verdict_id"])
    try:
        await _revalidate(ctx, verdict_id)
    except (RetryLater, PermanentError):
        raise
    except Exception as exc:
        capture(ec.FCK_REVALIDATION_FAILED, exc, verdict_id=verdict_id)
        raise RetryLater(_RETRY_S, "revalidation failed") from exc


async def _revalidate(ctx: JobContext, verdict_id: int) -> None:
    actor = _actor()
    now = datetime.now(UTC)
    async with ctx.db.transaction() as session:
        verdict = await session.get(Verdict, verdict_id)
        if verdict is None:
            log.info("factcheck.revalidate_skipped", verdict=verdict_id, reason="no_verdict")
            return
        group = (
            await session.execute(
                select(ClaimGroup).where(ClaimGroup.id == verdict.claim_group_id).with_for_update()
            )
        ).scalar_one()
        if (
            group.state not in _WAITING
            or group.active_verdict_id is not None
            or await _has_running_investigation(session, group, verdict)
        ):
            log.info("factcheck.revalidate_skipped", verdict=verdict_id, reason="not_waiting")
            return
        claim = await session.get(Claim, group.representative_claim_id)
        if claim is None:  # pragma: no cover - foreign key
            raise PermanentError("representative claim missing")
        content = await _latest_content(session, claim.id)

        fact = (
            await session.get(ReferenceFact, group.reference_fact_id)
            if group.reference_fact_id is not None and claim.claim_type == ClaimType.STATISTIC.value
            else None
        )
        if fact is not None:
            newer = await _newer_fact(session, fact)
            if newer is not None:
                group.reference_fact_id = newer.id
                group.band = None
                group.band_side = None
                result = references_uc.band_for_claim(claim, newer, ctx.settings)
                if result is not None:
                    band, side = result
                    group.band, group.band_side = band.value, side.value
                group.updated_at = func.now()
                session.add(
                    ClaimGroupEvent(
                        group_id=group.id,
                        kind=GroupEventKind.FACT_LINKED.value,
                        actor=actor.label,
                        reason="revalidation: newer reference fact",
                        data={
                            "reference_fact_id": str(newer.public_id),
                            "band": group.band,
                            "band_side": group.band_side,
                        },
                    )
                )
                fact = newer
            if await _try_band(session, ctx.settings, actor, group, fact, claim, content, now):
                log.info("factcheck.revalidated", verdict=verdict_id, how="band")
                return
        if content is None:
            raise PermanentError("no content to investigate for the group")
        await claims_uc.request_investigation(
            session,
            actor,
            group=group,
            content=content,
            level=group.level,
            requested_by=names.FACTCHECK_ACTOR,
            flags={"revalidation": True, "previous_verdict_id": verdict_id},
        )
        group.updated_at = func.now()
        log.info("factcheck.revalidated", verdict=verdict_id, how="investigation")
