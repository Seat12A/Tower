"""E3 candidate search: nearest claim groups by pivot vector (pgvector HNSW) and the
``Candidate`` objects the pure matching plan works on."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import Float, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain.factcheck.matching import Candidate
from tower.core.domain.factcheck.signatures import (
    NormalizedSignature,
    compare_signatures,
    normalize_signature,
)
from tower.core.domain.factcheck.values import ClaimGroupState
from tower.core.domain.factcheck.vectors import cosine
from tower.core.factcheck.schemas import ExtractedClaim
from tower.core.models.factcheck import Claim, ClaimGroup, ContentClaim, Verdict
from tower.core.models.ingestion import ContentItem

__all__ = [
    "NearRow",
    "build_candidates",
    "claim_signature",
    "nearest_groups",
    "post_dates",
    "signature_from_extracted",
]

EF_SEARCH = 40


@dataclass(frozen=True, slots=True)
class NearRow:
    """A group near the searched vector, with its representative claim."""

    group_id: int
    public_id: uuid.UUID
    distance: float
    level: str
    claim_id: int
    claim_lang: str
    claim_text: str
    signature: NormalizedSignature
    original_embedding: list[float] | None
    post_date: date | None
    active_verdict_id: int | None
    verdict_status: str | None
    verdict_expires_at: datetime | None


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def signature_from_extracted(
    extracted: ExtractedClaim, post_date: date | None
) -> NormalizedSignature:
    sig = extracted.signature
    return normalize_signature(
        claim_type=extracted.claim_type.value,
        subject=sig.subject,
        subject_qid=sig.subject_qid,
        predicate=sig.predicate,
        value_low=sig.value_low,
        value_high=sig.value_high,
        unit=sig.unit,
        unit_scale=sig.unit_scale,
        currency=sig.currency,
        value_date=_parse_date(sig.value_date),
        period_start=_parse_date(sig.period_start),
        period_end=_parse_date(sig.period_end),
        place=sig.place,
        primary_jurisdiction=extracted.primary_jurisdiction,
        polarity=sig.polarity.value,
        definition_variant=sig.definition_variant,
        comparison_base=sig.comparison_base,
        post_date=post_date,
    )


def claim_signature(claim: Claim, post_date: date | None) -> NormalizedSignature:
    """Normalized signature of a stored claim (never reads the author)."""
    return normalize_signature(
        claim_type=claim.claim_type,
        subject=claim.subject,
        subject_qid=claim.subject_qid,
        predicate=claim.predicate,
        value_low=claim.value_low,
        value_high=claim.value_high,
        unit=claim.unit,
        unit_scale=claim.unit_scale,
        currency=claim.currency,
        value_date=claim.value_date,
        period_start=claim.period_start,
        period_end=claim.period_end,
        place=claim.place,
        primary_jurisdiction=claim.primary_jurisdiction,
        polarity=claim.polarity,
        definition_variant=claim.definition_variant,
        comparison_base=claim.comparison_base,
        post_date=post_date,
    )


async def post_dates(session: AsyncSession, claim_ids: Sequence[int]) -> dict[int, date]:
    """Post date of each claim: the most recent ``created_at_platform`` of its contents,
    else the claim creation date."""
    if not claim_ids:
        return {}
    rows = (
        await session.execute(
            select(
                Claim.id,
                func.max(ContentItem.created_at_platform),
                Claim.created_at,
            )
            .select_from(Claim)
            .join(ContentClaim, ContentClaim.claim_id == Claim.id, isouter=True)
            .join(ContentItem, ContentItem.id == ContentClaim.content_id, isouter=True)
            .where(Claim.id.in_(list(claim_ids)))
            .group_by(Claim.id, Claim.created_at)
        )
    ).all()
    return {int(r[0]): (r[1] or r[2]).date() for r in rows}


async def nearest_groups(
    session: AsyncSession, pivot: Sequence[float], *, limit: int
) -> list[NearRow]:
    """The ``limit`` live groups nearest to ``pivot`` (cosine distance, HNSW index)."""
    await session.execute(text(f"SET LOCAL hnsw.ef_search = {EF_SEARCH}"))
    await session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    distance = ClaimGroup.representative_embedding.op("<=>", return_type=Float)(list(pivot))
    rows = (
        await session.execute(
            select(ClaimGroup, Claim, Verdict, distance.label("distance"))
            .join(Claim, Claim.id == ClaimGroup.representative_claim_id)
            .join(Verdict, Verdict.id == ClaimGroup.active_verdict_id, isouter=True)
            .where(
                ClaimGroup.representative_embedding.is_not(None),
                ClaimGroup.state != ClaimGroupState.MERGED.value,
            )
            .order_by(distance)
            .limit(limit)
        )
    ).all()
    dates = await post_dates(session, [r[1].id for r in rows])
    out: list[NearRow] = []
    for group, claim, verdict, dist in rows:
        day = dates.get(claim.id)
        out.append(
            NearRow(
                group_id=group.id,
                public_id=group.public_id,
                distance=float(dist),
                level=group.level,
                claim_id=claim.id,
                claim_lang=claim.lang_original,
                claim_text=claim.text_original,
                signature=claim_signature(claim, day),
                original_embedding=claim.embedding,
                post_date=day,
                active_verdict_id=group.active_verdict_id,
                verdict_status=verdict.status if verdict is not None else None,
                verdict_expires_at=verdict.expires_at if verdict is not None else None,
            )
        )
    return out


def build_candidates(
    rows: Sequence[NearRow],
    *,
    claim_lang: str,
    signature: NormalizedSignature,
    original_embedding: Sequence[float] | None,
    current_period_days: int,
) -> list[Candidate]:
    """``similarity_pivot = 1 - distance``; ``similarity_original`` only for the same
    language (exact cosine between the original vectors)."""
    out: list[Candidate] = []
    for row in rows:
        same_lang = row.claim_lang.casefold() == claim_lang.casefold()
        original: float | None = None
        if same_lang and original_embedding is not None and row.original_embedding is not None:
            original = cosine(original_embedding, row.original_embedding)
        out.append(
            Candidate(
                group_id=row.group_id,
                claim_lang=row.claim_lang,
                similarity_pivot=1.0 - row.distance,
                similarity_original=original,
                comparison=compare_signatures(
                    signature, row.signature, current_period_days=current_period_days
                ),
                has_active_verdict=row.active_verdict_id is not None
                and row.verdict_status == "active",
            )
        )
    return out
