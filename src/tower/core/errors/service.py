"""Consultation et résolution des erreurs stockées (API, CLI, bot)."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain import error_codes as ec
from tower.core.domain.errors import NotFound
from tower.core.domain.security import Actor, Permission
from tower.core.events.outbox import publish
from tower.core.models.errors import ErrorReport


async def list_reports(
    session: AsyncSession,
    actor: Actor,
    *,
    include_resolved: bool = False,
    code: str | None = None,
    limit: int = 50,
) -> Sequence[ErrorReport]:
    actor.require(Permission.ERRORS_READ)
    stmt = select(ErrorReport).order_by(ErrorReport.last_seen.desc()).limit(min(limit, 200))
    if not include_resolved:
        stmt = stmt.where(ErrorReport.resolved_at.is_(None))
    if code:
        stmt = stmt.where(ErrorReport.code == code)
    return (await session.scalars(stmt)).all()


async def get_report(session: AsyncSession, actor: Actor, fingerprint: str) -> ErrorReport:
    actor.require(Permission.ERRORS_READ)
    report = await session.get(ErrorReport, fingerprint)
    if report is None:
        raise NotFound("Erreur introuvable", code=ec.GEN_NOT_FOUND, fingerprint=fingerprint)
    return report


async def resolve(
    session: AsyncSession, actor: Actor, fingerprint: str, *, note: str | None = None
) -> ErrorReport:
    actor.require(Permission.ERRORS_MANAGE)
    report = await session.get(ErrorReport, fingerprint, with_for_update=True)
    if report is None:
        raise NotFound("Erreur introuvable", code=ec.GEN_NOT_FOUND, fingerprint=fingerprint)
    report.resolved_at = func.now()
    report.resolved_by = actor.label
    report.resolution_note = note
    await session.flush()
    await session.refresh(report)
    await publish(
        session,
        "incident.resolved",
        {"fingerprint": fingerprint, "code": report.code, "note": note},
        aggregate_type="error_report",
        aggregate_id=fingerprint,
        actor=actor.label,
    )
    return report
