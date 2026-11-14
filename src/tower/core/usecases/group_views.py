"""Read models of claim groups, reference facts and value bands (gate and bot).

Everything here is read-only: ids are public UUIDs as strings (internal ids never leave).
Writes live in ``groups.py`` and ``references.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain import error_codes as ec
from tower.core.domain.errors import NotFound
from tower.core.domain.factcheck.urls import content_url
from tower.core.domain.security import Actor, Permission
from tower.core.models.factcheck import (
    Claim,
    ClaimGroup,
    ClaimGroupMember,
    ContentClaim,
    Verdict,
)
from tower.core.models.factcheck_v1 import ClaimGroupEvent, ReferenceFact, VerdictBand
from tower.core.models.ingestion import ContentItem

__all__ = [
    "band_label",
    "band_view",
    "draft_group_info",
    "fact_view",
    "get_fact",
    "get_group",
    "list_bands",
    "list_facts",
    "list_members",
]

JOURNAL_LIMIT = 50
OTHER_POSTS_LIMIT = 10


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _num(value: Decimal | None) -> str | None:
    return None if value is None else format(value.normalize(), "f")


def band_label(band: str, side: str) -> str:
    """Short English label of a value band (``within 1 tau, above the fact``)."""
    names = {
        "contains": "contains the fact",
        "within_1_tau": "within 1 tau of the fact",
        "within_3_tau": "within 3 tau of the fact",
        "beyond": "beyond 3 tau of the fact",
    }
    text = names.get(band, band.replace("_", " "))
    return text if side in ("none", "") else f"{text} ({side})"


def fact_view(fact: ReferenceFact) -> dict[str, Any]:
    return {
        "id": str(fact.public_id),
        "indicator": fact.indicator,
        "label": fact.label,
        "definition_variant": fact.definition_variant,
        "value": _num(fact.value),
        "unit": fact.unit,
        "unit_scale": fact.unit_scale,
        "period_start": fact.period_start.isoformat(),
        "period_end": fact.period_end.isoformat(),
        "territory": fact.territory,
        "source_url": fact.source_url,
        "source_name": fact.source_name,
        "published_at": fact.published_at.isoformat(),
        "next_publication": _iso_date(fact.next_publication),
        "status": fact.status,
        "origin": fact.origin,
        "created_by": fact.created_by,
        "validated_by": fact.validated_by,
        "validated_at": _iso(fact.validated_at),
    }


def _iso_date(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def band_view(
    row: VerdictBand, fact_public_id: uuid.UUID, source_verdict: uuid.UUID | None
) -> dict[str, Any]:
    return {
        "id": str(row.public_id),
        "fact_id": str(fact_public_id),
        "band": row.band,
        "side": row.side,
        "label": band_label(row.band, row.side),
        "veracity": row.veracity,
        "context_need": row.context_need,
        "context_types": list(row.context_types or []),
        "source_verdict_id": str(source_verdict) if source_verdict is not None else None,
        "status": row.status,
        "validated_by": row.validated_by,
        "validated_at": _iso(row.validated_at),
        "revoked_by": row.revoked_by,
        "revoked_at": _iso(row.revoked_at),
    }


async def _group_by_public_id(session: AsyncSession, public_id: uuid.UUID) -> ClaimGroup:
    group = await session.scalar(select(ClaimGroup).where(ClaimGroup.public_id == public_id))
    if group is None:
        raise NotFound("Claim group not found", code=ec.FCK_GROUP_NOT_FOUND, group=str(public_id))
    return group


def _signature(claim: Claim) -> dict[str, Any]:
    return {
        "claim_type": claim.claim_type,
        "subject": claim.subject,
        "predicate": claim.predicate,
        "value_low": _num(claim.value_low),
        "value_high": _num(claim.value_high),
        "unit": claim.unit,
        "currency": claim.currency,
        "period_start": _iso_date(claim.period_start),
        "period_end": _iso_date(claim.period_end),
        "place": claim.place,
        "polarity": claim.polarity,
    }


async def list_members(
    session: AsyncSession,
    actor: Actor,
    group_id: uuid.UUID,
    *,
    include_removed: bool = False,
) -> list[dict[str, Any]]:
    """Claims of a group with their signature summary and the contents they come from."""
    actor.require(Permission.DOSSIER_READ)
    group = await _group_by_public_id(session, group_id)
    return await _members(session, group, include_removed=include_removed)


async def _members(
    session: AsyncSession, group: ClaimGroup, *, include_removed: bool
) -> list[dict[str, Any]]:
    stmt = (
        select(ClaimGroupMember, Claim)
        .join(Claim, Claim.id == ClaimGroupMember.claim_id)
        .where(ClaimGroupMember.group_id == group.id)
        .order_by(ClaimGroupMember.added_at, Claim.id)
    )
    if not include_removed:
        stmt = stmt.where(ClaimGroupMember.removed_at.is_(None))
    rows = (await session.execute(stmt)).all()
    claim_ids = [c.id for _, c in rows]
    contents: dict[int, list[dict[str, Any]]] = {}
    if claim_ids:
        result = await session.execute(
            select(ContentClaim.claim_id, ContentItem)
            .join(ContentItem, ContentItem.id == ContentClaim.content_id)
            .where(ContentClaim.claim_id.in_(claim_ids))
            .order_by(ContentItem.id)
        )
        for claim_id, content in result.all():
            contents.setdefault(claim_id, []).append(
                {
                    "platform": content.platform,
                    "external_id": content.external_id,
                    "url": content_url(content.platform, content.external_id),
                    "posted_at": _iso(content.created_at_platform),
                }
            )
    return [
        {
            "claim_id": str(claim.public_id),
            "text_original": claim.text_original,
            "lang": claim.lang_original,
            "text_fr": claim.text_fr,
            "representative": claim.id == group.representative_claim_id,
            "signature": _signature(claim),
            "contents": contents.get(claim.id, []),
            "added_at": _iso(member.added_at),
            "removed_at": _iso(member.removed_at),
            "removed_reason": member.reason,
        }
        for member, claim in rows
    ]


async def _public_ids(session: AsyncSession, model: Any, ids: set[int]) -> dict[int, uuid.UUID]:
    if not ids:
        return {}
    rows = await session.execute(select(model.id, model.public_id).where(model.id.in_(ids)))
    return {i: p for i, p in rows.all()}


async def _journal(session: AsyncSession, group: ClaimGroup) -> list[dict[str, Any]]:
    events = list(
        await session.scalars(
            select(ClaimGroupEvent)
            .where(ClaimGroupEvent.group_id == group.id)
            .order_by(ClaimGroupEvent.id.desc())
            .limit(JOURNAL_LIMIT)
        )
    )
    groups = await _public_ids(
        session, ClaimGroup, {e.other_group_id for e in events if e.other_group_id is not None}
    )
    claims = await _public_ids(session, Claim, {c for e in events for c in e.claim_ids})
    return [
        {
            "kind": e.kind,
            "other_group_id": (
                str(groups[e.other_group_id]) if e.other_group_id in groups else None
            ),
            "claim_ids": [str(claims[c]) for c in e.claim_ids if c in claims],
            "from_level": e.from_level,
            "to_level": e.to_level,
            "actor": e.actor,
            "reason": e.reason,
            # Internal integer ids are never exposed.
            "data": {
                k: v
                for k, v in (e.data or {}).items()
                if not (k.endswith("_id") and isinstance(v, int))
            },
            "created_at": _iso(e.created_at),
        }
        for e in events
    ]


async def _active_verdict_view(session: AsyncSession, group: ClaimGroup) -> dict[str, Any] | None:
    if group.active_verdict_id is None:
        return None
    verdict = await session.get(Verdict, group.active_verdict_id)
    if verdict is None:
        return None
    reused = (
        await session.scalar(
            select(Verdict.public_id).where(Verdict.id == verdict.reused_from_verdict_id)
        )
        if verdict.reused_from_verdict_id is not None
        else None
    )
    return {
        "id": str(verdict.public_id),
        "status": verdict.status,
        "veracity": verdict.veracity,
        "expires_at": _iso(verdict.expires_at),
        "reuse_rule": verdict.reuse_rule,
        "reused_from": str(reused) if reused is not None else None,
    }


async def get_group(session: AsyncSession, actor: Actor, group_id: uuid.UUID) -> dict[str, Any]:
    """Group detail: state, fact and band, active verdict, active members, journal."""
    actor.require(Permission.DOSSIER_READ)
    group = await _group_by_public_id(session, group_id)
    merged_into = (
        await session.scalar(
            select(ClaimGroup.public_id).where(ClaimGroup.id == group.merged_into_id)
        )
        if group.merged_into_id is not None
        else None
    )
    fact = (
        await session.get(ReferenceFact, group.reference_fact_id)
        if group.reference_fact_id is not None
        else None
    )
    return {
        "id": str(group.public_id),
        "level": group.level,
        "state": group.state,
        "reach": group.reach,
        "merged_into": str(merged_into) if merged_into is not None else None,
        "reference_fact": fact_view(fact) if fact is not None else None,
        "band": group.band,
        "band_side": group.band_side,
        "active_verdict": await _active_verdict_view(session, group),
        "members": await _members(session, group, include_removed=False),
        "journal": await _journal(session, group),
    }


async def get_fact(session: AsyncSession, actor: Actor, fact_id: uuid.UUID) -> dict[str, Any]:
    actor.require(Permission.DOSSIER_READ)
    fact = await session.scalar(select(ReferenceFact).where(ReferenceFact.public_id == fact_id))
    if fact is None:
        raise NotFound("Reference fact not found", code=ec.FCK_FACT_NOT_FOUND, fact=str(fact_id))
    return fact_view(fact)


async def list_facts(
    session: AsyncSession,
    actor: Actor,
    *,
    status: str | None = None,
    indicator: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    actor.require(Permission.DOSSIER_READ)
    stmt = select(ReferenceFact).order_by(ReferenceFact.id.desc()).limit(limit)
    if status:
        stmt = stmt.where(ReferenceFact.status == status)
    if indicator:
        stmt = stmt.where(ReferenceFact.indicator == indicator)
    return [fact_view(f) for f in await session.scalars(stmt)]


async def list_bands(
    session: AsyncSession, actor: Actor, fact_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Every band of a fact (active first, then revoked), newest first."""
    actor.require(Permission.DOSSIER_READ)
    fact = await session.scalar(select(ReferenceFact).where(ReferenceFact.public_id == fact_id))
    if fact is None:
        raise NotFound("Reference fact not found", code=ec.FCK_FACT_NOT_FOUND, fact=str(fact_id))
    rows = (
        await session.execute(
            select(VerdictBand, Verdict.public_id)
            .outerjoin(Verdict, Verdict.id == VerdictBand.source_verdict_id)
            .where(VerdictBand.reference_fact_id == fact.id)
            .order_by((VerdictBand.status == "active").desc(), VerdictBand.id.desc())
        )
    ).all()
    return [band_view(b, fact.public_id, v) for b, v in rows]


async def draft_group_info(
    session: AsyncSession,
    group: ClaimGroup,
    claim: Claim | None,
    *,
    content_id: int | None,
) -> dict[str, Any]:
    """Group-related fields of a draft view: group, other posts, platforms, reusable
    verdict suggestion, reference fact and band."""
    members = int(
        await session.scalar(
            select(func.count())
            .select_from(ClaimGroupMember)
            .where(ClaimGroupMember.group_id == group.id, ClaimGroupMember.removed_at.is_(None))
        )
        or 0
    )
    contents = list(
        await session.scalars(
            select(ContentItem)
            .join(ContentClaim, ContentClaim.content_id == ContentItem.id)
            .join(ClaimGroupMember, ClaimGroupMember.claim_id == ContentClaim.claim_id)
            .where(ClaimGroupMember.group_id == group.id, ClaimGroupMember.removed_at.is_(None))
            .distinct()
            .order_by(ContentItem.id)
        )
    )
    others = [c for c in contents if c.id != content_id]
    platforms = sorted({c.platform for c in contents})

    reusable: dict[str, Any] | None = None
    related = await session.scalar(
        select(ClaimGroupEvent.other_group_id)
        .where(
            ClaimGroupEvent.group_id == group.id,
            ClaimGroupEvent.kind == "created",
            ClaimGroupEvent.other_group_id.is_not(None),
        )
        .order_by(ClaimGroupEvent.id)
        .limit(1)
    )
    if related is not None:
        row = (
            await session.execute(
                select(Verdict, ClaimGroup.public_id)
                .join(ClaimGroup, ClaimGroup.active_verdict_id == Verdict.id)
                .where(ClaimGroup.id == related, Verdict.status == "active")
            )
        ).first()
        if row is not None:
            reusable = {
                "id": str(row[0].public_id),
                "veracity": row[0].veracity,
                "group_id": str(row[1]),
            }

    fact_info: dict[str, Any] | None = None
    if group.reference_fact_id is not None:
        fact = await session.get(ReferenceFact, group.reference_fact_id)
        if fact is not None:
            fact_info = {
                "indicator": fact.indicator,
                "label": fact.label,
                "value": _num(fact.value),
                "unit": fact.unit,
                "period_start": fact.period_start.isoformat(),
                "period_end": fact.period_end.isoformat(),
                "territory": fact.territory,
                "status": fact.status,
            }
    band_info: dict[str, Any] | None = None
    if claim is not None and group.band is not None and group.band_side is not None:
        band_info = {
            "band": group.band,
            "side": group.band_side,
            "label": band_label(group.band, group.band_side),
        }
    return {
        "group": {"id": str(group.public_id), "level": group.level, "members": members},
        "other_posts": [
            {
                "platform": c.platform,
                "external_id": c.external_id,
                "url": content_url(c.platform, c.external_id),
                "posted_at": _iso(c.created_at_platform),
            }
            for c in others[:OTHER_POSTS_LIMIT]
        ],
        "platforms": platforms,
        "reusable_verdict": reusable,
        "reference_fact": fact_info,
        "band": band_info,
    }
