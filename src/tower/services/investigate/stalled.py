"""Stalled investigations: a stage left without any job to run it.

A stage job can end without marking its investigation ``failed``: a timeout on its last
attempt (the runner fails the job, the handler never sees it), a ``PermanentError`` raised
by a step, a lost job. The investigation would then show the same stage forever. This
periodic task marks it ``failed`` with the last job error, so ``/factchecks`` shows the
cause and a new request starts over.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, Integer, and_, cast, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.config import Settings
from tower.core.domain import error_codes as ec
from tower.core.domain.factcheck.values import InvestigationStage
from tower.core.domain.security import Actor
from tower.core.errors import capture
from tower.core.factcheck import names
from tower.core.logging import get_logger
from tower.core.models.factcheck import Investigation
from tower.core.models.jobs import Job
from tower.core.usecases import investigations as uc

__all__ = ["STALL_AFTER", "fail_stalled"]

log = get_logger(__name__)

STALL_AFTER = timedelta(minutes=30)
"""An investigation untouched this long with no queued or running job is stalled. Far
above a stage's run time (240 s) and the lease (300 s)."""

_LIMIT = 50
_FINAL = (
    InvestigationStage.ASSEMBLED.value,
    InvestigationStage.FAILED.value,
)
_ACTOR = Actor.system(names.FACTCHECK_ACTOR)


def _job_of(inv_id: Any) -> ColumnElement[bool]:
    key: ColumnElement[int] = cast(Job.payload["investigation_id"].astext, Integer)
    return key.__eq__(inv_id)


async def fail_stalled(session: AsyncSession, settings: Settings) -> int:
    """Mark stalled investigations ``failed``. Returns how many were."""
    now = datetime.now(UTC)
    active = exists().where(
        Job.status.in_(("queued", "running")),
        Job.payload.has_key("investigation_id"),
        _job_of(Investigation.id),
    )
    stalled = (
        await session.scalars(
            select(Investigation)
            .where(
                Investigation.stage.not_in(_FINAL),
                Investigation.updated_at < now - STALL_AFTER,
                ~active,
            )
            .order_by(Investigation.id)
            .limit(_LIMIT)
            .with_for_update(skip_locked=True)
        )
    ).all()
    count = 0
    for inv in stalled:
        last = await session.scalar(
            select(Job.last_error)
            .where(and_(Job.payload.has_key("investigation_id"), _job_of(inv.id)))
            .order_by(Job.id.desc())
            .limit(1)
        )
        reason = (last or "no job left to run this stage")[:300]
        capture(
            ec.FCK_STAGE_FAILED,
            message="Investigation stalled",
            stage=inv.stage,
            investigation_id=inv.id,
            error_type="Stalled",
        )
        if await uc.fail_investigation(
            session, _ACTOR, inv, error=f"stalled at {inv.stage}: {reason}"
        ):
            count += 1
    if count:
        log.warning("investigate.stalled_failed", count=count)
    return count
