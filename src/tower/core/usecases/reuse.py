"""Verdict inheritance through a validated value band (Q9, rule 3). Shared by E3
(``record_extraction``) and the revalidation job.

The caller has already decided that the band applies (validated fact, active band,
level other than N4, ``can_reuse`` true). This use case only writes: a new active verdict
for the group, which points to the band's source verdict, and ``verdict.reused``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain.factcheck.values import ClaimGroupState, ReuseRule, VerdictStatus
from tower.core.domain.security import Actor, Permission
from tower.core.events.outbox import publish
from tower.core.factcheck import names
from tower.core.logging import get_logger
from tower.core.models.factcheck import Claim, ClaimGroup, Verdict
from tower.core.models.factcheck_v1 import ReferenceFact, VerdictBand

__all__ = ["inherit_band_verdict"]

log = get_logger(__name__)


def _expiry(source: Verdict | None, fact: ReferenceFact, now: datetime) -> datetime | None:
    candidates: list[datetime] = []
    if source is not None and source.expires_at is not None:
        candidates.append(source.expires_at)
    if fact.next_publication is not None:
        candidates.append(datetime.combine(fact.next_publication, time.min, tzinfo=UTC))
    return min(candidates) if candidates else None


async def inherit_band_verdict(
    session: AsyncSession,
    actor: Actor,
    *,
    group: ClaimGroup,
    band: VerdictBand,
    fact: ReferenceFact,
    claim: Claim,
    content_id: int | None,
    now: datetime | None = None,
) -> Verdict:
    """Give ``group`` an active verdict inherited from ``band`` (never reviewed again,
    never counted in the draft quota) and publish ``verdict.reused`` (rule ``band``)."""
    actor.require(Permission.FACTCHECK_REQUEST)
    now = now or datetime.now(UTC)
    source = await session.get(Verdict, band.source_verdict_id) if band.source_verdict_id else None
    verdict = Verdict(
        public_id=uuid.uuid4(),
        claim_group_id=group.id,
        veracity=band.veracity,
        context_need=band.context_need,
        context_types=list(band.context_types),
        status=VerdictStatus.ACTIVE.value,
        flags={
            "inherited": True,
            "band": band.band,
            "band_side": band.side,
            "reference_fact_id": str(fact.public_id),
            "band_id": str(band.public_id),
        },
        validated_at=now,
        validated_by=names.FACTCHECK_ACTOR,
        expires_at=_expiry(source, fact, now),
        reused_from_verdict_id=source.id if source is not None else None,
        reuse_rule=ReuseRule.BAND.value,
    )
    session.add(verdict)
    await session.flush()
    group.active_verdict_id = verdict.id
    group.state = ClaimGroupState.PASSIVE.value
    group.updated_at = func.now()
    await publish(
        session,
        names.VERDICT_REUSED,
        {
            "verdict_id": str(verdict.public_id),
            "group_id": str(group.public_id),
            "claim_id": str(claim.public_id),
            "content_id": str(content_id) if content_id is not None else None,
            "rule": ReuseRule.BAND.value,
            "origin_verdict_id": str(source.public_id) if source is not None else None,
        },
        aggregate_type="claim_group",
        aggregate_id=str(group.public_id),
        actor=actor.label,
    )
    log.info("factcheck.verdict_inherited", group=group.id, band=band.band, side=band.side)
    return verdict
