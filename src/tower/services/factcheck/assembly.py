"""E10 helpers: final confidence, platform reads with a provider budget, quota counting."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.budget import BudgetRefused, reserve_budget, settle_budget
from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain import error_codes as ec
from tower.core.domain.factcheck.confidence import ConfidenceBand, band, compute_confidence
from tower.core.domain.factcheck.independence import SourceRef, assess_independence
from tower.core.domain.factcheck.values import Agreement, Veracity
from tower.core.domain.text import content_hash, normalize
from tower.core.errors import capture
from tower.core.factcheck import names, runtime
from tower.core.factcheck.signals import build_signals
from tower.core.models.factcheck import Citation, EvidencePassage, Investigation, Source, Verdict
from tower.core.ports.platforms import ContentItem as PlatformItem
from tower.core.ports.platforms import PlatformError, PlatformNotConfigured, UnsupportedOperation

__all__ = [
    "QUOTA_LOCK",
    "agreement_of",
    "final_confidence",
    "lock_quota",
    "read_item",
    "recheck_flags",
    "today_count",
    "today_start",
]

QUOTA_LOCK = "factcheck:draft_quota"
_ACTOR = names.FACTCHECK_ACTOR


def today_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return datetime(now.year, now.month, now.day, tzinfo=UTC)


async def lock_quota(session: AsyncSession) -> None:
    """Serialize the quota decisions of concurrent assemblies and drains."""
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": QUOTA_LOCK})


async def today_count(session: AsyncSession, now: datetime | None = None) -> int:
    """Drafts that entered the review queue since 00:00 UTC (every status counts)."""
    return int(
        await session.scalar(
            select(func.count()).select_from(Verdict).where(Verdict.drafted_at >= today_start(now))
        )
        or 0
    )


def agreement_of(flags: dict[str, Any], column: str | None) -> Agreement | None:
    value = flags.get("agreement") or column
    return Agreement(value) if value else None


async def final_confidence(
    session: AsyncSession,
    settings: Settings,
    verdict: Verdict,
    investigation: Investigation,
    claim_type: str,
) -> tuple[float, ConfidenceBand]:
    """Confidence of the draft from its valid citations and the stored flags (E10)."""
    flags: dict[str, Any] = dict(verdict.flags or {})
    rows = (
        await session.execute(
            select(
                EvidencePassage.label,
                EvidencePassage.origin,
                EvidencePassage.simhash,
                Source.domain,
                Source.tier,
            )
            .join(Source, Source.id == EvidencePassage.source_id)
            .where(
                EvidencePassage.id.in_(
                    select(Citation.passage_id).where(
                        Citation.verdict_id == verdict.id, Citation.valid.is_(True)
                    )
                )
            )
            .order_by(EvidencePassage.id)
        )
    ).all()
    refs = [
        SourceRef(
            passage_id=r.label,
            origin=r.origin,
            domain=r.domain,
            tier=r.tier,
            simhash=int(r.simhash),
        )
        for r in rows
    ]
    independence = assess_independence(refs)
    second_pass = {"concordant": True, "discordant": False}.get(str(flags.get("second_pass")))
    signals = build_signals(
        veracity=Veracity(verdict.veracity),
        claim_type=claim_type,
        pitfalls=(investigation.framing or {}).get("known_pitfalls") or [],
        context_types=verdict.context_types or [],
        cited=refs,
        primary_passage_ids=flags.get("primary_passages") or [],
        independence=independence,
        conflicting_sources=bool(flags.get("conflicting_sources")),
        agreement=agreement_of(flags, investigation.agreement),
        second_pass=second_pass,
    )
    coefficients, thresholds = runtime.confidence_settings(settings)
    confidence = compute_confidence(signals, coefficients, thresholds)
    return confidence, band(confidence, thresholds)


async def read_item(
    db: Database, settings: Settings, platform: str, external_id: str, *, ref: str
) -> PlatformItem | None:
    """One platform read under a ``provider:<platform>`` budget reservation, settled at the
    real cost (nothing for a missing item). Raises ``PlatformNotConfigured``,
    ``BudgetRefused`` or the platform's own errors."""
    client = runtime.get_platform_client(settings, platform)
    holds = await reserve_budget(
        db,
        (f"provider:{platform}",),
        client.cost_per_read_usd_micros,
        ref=ref,
        now=datetime.now(UTC),
        ttl_s=settings.budget_reservation_ttl_s,
    )
    cost = 0
    try:
        item = await client.get_item(external_id)
        if item is not None:
            cost = client.cost_per_read_usd_micros
    finally:
        async with db.transaction() as session:
            await settle_budget(session, holds, cost, actor=_ACTOR)
    return item


async def recheck_flags(
    db: Database,
    settings: Settings,
    *,
    platform: str,
    external_id: str,
    stored_hash: bytes,
    investigation_id: int,
) -> dict[str, Any]:
    """Flags of the representative post recheck; never raises (a recheck never blocks)."""
    try:
        item = await read_item(
            db, settings, platform, external_id, ref=f"factcheck:recheck:{investigation_id}"
        )
    except (PlatformNotConfigured, BudgetRefused):
        return {"recheck": "skipped"}
    except (PlatformError, UnsupportedOperation) as exc:
        capture(ec.FCK_PLATFORM_UNAVAILABLE, exc, platform=platform, investigation=investigation_id)
        return {"recheck": "failed"}
    if item is None:
        capture(
            ec.FCK_POST_CHANGED,
            message="Post deleted",
            change="deleted",
            platform=platform,
            investigation=investigation_id,
        )
        return {"recheck": "ok", "post_deleted": True}
    if content_hash(normalize(item.text)) != stored_hash:
        capture(
            ec.FCK_POST_CHANGED,
            message="Post modified",
            change="modified",
            platform=platform,
            investigation=investigation_id,
        )
        return {"recheck": "ok", "post_modified": True}
    return {"recheck": "ok"}
