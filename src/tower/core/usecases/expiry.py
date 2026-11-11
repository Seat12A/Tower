"""Expiry of verdicts and provisional notes (docs/factcheck/pipeline.md, "Expiration").

An expired verdict is never reused: its group loses its active verdict and goes back to
revalidation (job ``factcheck.revalidate``), the notes of the verdict expire with it.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain.factcheck.values import (
    ClaimGroupState,
    NoteStatus,
    Regime,
    ReuseRule,
    VerdictStatus,
)
from tower.core.domain.security import Actor, Permission
from tower.core.events.outbox import publish
from tower.core.factcheck import names
from tower.core.jobs.queue import enqueue
from tower.core.logging import get_logger
from tower.core.models.factcheck import Claim, ClaimGroup, ContextNote, Verdict

__all__ = ["expire_provisional_notes", "expire_verdicts"]

log = get_logger(__name__)

_LIVE_STATES = (ClaimGroupState.VERIFIED.value, ClaimGroupState.PASSIVE.value)
_LIVE_NOTES = (NoteStatus.DRAFT.value, NoteStatus.VALIDATED.value)


async def expire_verdicts(
    session: AsyncSession, actor: Actor, *, now: datetime, limit: int = 200
) -> int:
    """Expire the active verdicts whose ``expires_at`` has passed. Returns how many.

    Group: no active verdict, state ``to_revalidate`` (``queued`` for a verdict inherited
    through a band: it is revalidated like any other group). One ``verdict.expired`` and
    one ``factcheck.revalidate`` job (deduplicated) per verdict, in the same transaction.
    """
    actor.require(Permission.FACTCHECK_REQUEST)
    verdicts = (
        await session.scalars(
            select(Verdict)
            .where(
                Verdict.status == VerdictStatus.ACTIVE.value,
                Verdict.expires_at.is_not(None),
                Verdict.expires_at <= now,
            )
            .order_by(Verdict.expires_at, Verdict.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).all()
    for verdict in verdicts:
        group = (
            await session.execute(
                select(ClaimGroup).where(ClaimGroup.id == verdict.claim_group_id).with_for_update()
            )
        ).scalar_one()
        expired_at = verdict.expires_at or now  # never None: filtered above
        verdict.status = VerdictStatus.EXPIRED.value
        verdict.updated_at = func.now()
        if group.active_verdict_id == verdict.id:
            group.active_verdict_id = None
        if group.state in _LIVE_STATES:
            inherited = verdict.reuse_rule == ReuseRule.BAND.value
            group.state = (
                ClaimGroupState.QUEUED.value if inherited else ClaimGroupState.TO_REVALIDATE.value
            )
        group.updated_at = func.now()
        notes = (
            await session.scalars(
                select(ContextNote)
                .where(ContextNote.verdict_id == verdict.id, ContextNote.status.in_(_LIVE_NOTES))
                .with_for_update()
            )
        ).all()
        for note in notes:
            note.status = NoteStatus.EXPIRED.value
            note.updated_at = func.now()
        claim_type = await session.scalar(
            select(Claim.claim_type).where(Claim.id == group.representative_claim_id)
        )
        await publish(
            session,
            names.VERDICT_EXPIRED,
            {
                "verdict_id": str(verdict.public_id),
                "group_id": str(group.public_id),
                "claim_type": claim_type,
                "expires_at": expired_at.isoformat(),
            },
            aggregate_type="claim_group",
            aggregate_id=str(group.public_id),
            actor=actor.label,
        )
        await enqueue(
            session,
            names.FC_BULK,
            names.JOB_REVALIDATE,
            {"verdict_id": verdict.id},
            dedupe_key=names.revalidate_key(verdict.id),
        )
    await session.flush()
    if verdicts:
        log.info("factcheck.verdicts_expired", count=len(verdicts))
    return len(verdicts)


async def expire_provisional_notes(
    session: AsyncSession, actor: Actor, *, now: datetime, limit: int = 500
) -> int:
    """Notes of regime ``provisional`` past their ``expires_at`` become ``expired``: they
    are never shown or inherited afterwards. Returns how many."""
    actor.require(Permission.FACTCHECK_REQUEST)
    notes = (
        await session.scalars(
            select(ContextNote)
            .where(
                ContextNote.regime == Regime.PROVISIONAL.value,
                ContextNote.status == NoteStatus.VALIDATED.value,
                ContextNote.expires_at.is_not(None),
                ContextNote.expires_at <= now,
            )
            .order_by(ContextNote.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).all()
    for note in notes:
        note.status = NoteStatus.EXPIRED.value
        note.updated_at = func.now()
    await session.flush()
    if notes:
        log.info("factcheck.provisional_notes_expired", count=len(notes))
    return len(notes)
