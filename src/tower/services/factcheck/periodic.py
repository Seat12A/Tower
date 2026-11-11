"""Periodic tasks of the ``factcheck`` service (singleton, one transaction each)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.config import Settings
from tower.core.domain.factcheck.quota import DraftCandidate, select_for_review
from tower.core.domain.factcheck.values import Veracity
from tower.core.domain.security import Actor
from tower.core.factcheck import names
from tower.core.models.factcheck import ClaimGroup, Investigation, Verdict
from tower.core.models.ingestion import ContentItem
from tower.core.usecases.assembly import open_draft
from tower.services.factcheck import assembly as asm

__all__ = ["drain_quota", "purge_source_cache"]

_MAX_CANDIDATES = 500


async def drain_quota(session: AsyncSession, settings: Settings) -> int:
    """Open the dossiers of the waiting drafts that fit in today's remaining quota,
    best candidates first (``quota.select_for_review``). Returns how many were opened."""
    await asm.lock_quota(session)
    slots = settings.fc_draft_daily_quota - await asm.today_count(session)
    if slots <= 0:
        return 0
    rows = (
        await session.execute(
            select(Verdict, Investigation, ClaimGroup.level, ContentItem.metrics)
            .join(Investigation, Investigation.id == Verdict.investigation_id)
            .join(ClaimGroup, ClaimGroup.id == Verdict.claim_group_id)
            .outerjoin(ContentItem, ContentItem.id == Investigation.content_id)
            .where(
                Verdict.status == "draft",
                Verdict.drafted_at.is_(None),
                Investigation.stage == "assembled",
            )
            .order_by(Verdict.id)
            .limit(_MAX_CANDIDATES)
            .with_for_update(of=Verdict, skip_locked=True)
        )
    ).all()
    by_key: dict[str, tuple[Verdict, Investigation]] = {}
    candidates: list[DraftCandidate] = []
    for verdict, investigation, level, metrics in rows:
        flags: dict[str, Any] = dict(verdict.flags or {})
        impressions = (metrics or {}).get("impressions")
        key = str(verdict.id)
        by_key[key] = (verdict, investigation)
        candidates.append(
            DraftCandidate(
                key=key,
                veracity=Veracity(verdict.veracity),
                confidence=verdict.confidence or 0.0,
                agreement=asm.agreement_of(flags, investigation.agreement),
                conflicting_sources=bool(flags.get("conflicting_sources")),
                level=level,
                reach=int(impressions) if isinstance(impressions, int | float) else 0,
                ready_at=verdict.created_at,
            )
        )
    actor = Actor.system(names.FACTCHECK_ACTOR)
    opened = 0
    for candidate in select_for_review(candidates, slots=slots):
        verdict, investigation = by_key[candidate.key]
        await open_draft(session, actor, verdict, investigation)
        opened += 1
    return opened


async def purge_source_cache(session: AsyncSession, settings: Settings) -> int:
    """Delete the expired rows of the knowledge source cache (written by Atlas)."""
    from tower.core.adapters.knowledge.cache import purge_expired

    return await purge_expired(session, datetime.now(UTC))
