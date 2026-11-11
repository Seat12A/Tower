"""Claims of a content: recording an extraction (E2) and promoting a group to N4.

Both write through the use case rules: permission check, domain rules, events published
in the same transaction as the rows (see docs/factcheck/pipeline.md).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain.factcheck.opinion import initial_depth
from tower.core.domain.factcheck.values import UNVERIFIABLE_CLAIM_TYPES, ClaimGroupState, ClaimType
from tower.core.domain.security import Actor, Permission
from tower.core.events.outbox import publish
from tower.core.factcheck import names
from tower.core.factcheck.schemas import ExtractedClaim
from tower.core.logging import get_logger
from tower.core.models.factcheck import (
    Claim,
    ClaimGroup,
    ClaimGroupMember,
    ContentClaim,
    Investigation,
)
from tower.core.models.ingestion import ContentItem

__all__ = ["ExtractionRecord", "promote_to_n4", "record_extraction", "request_investigation"]

log = get_logger(__name__)

FRAMING = "framing"
FAILED = "failed"


@dataclass(slots=True)
class ExtractionRecord:
    claim_ids: list[int] = field(default_factory=list)
    group_ids: list[int] = field(default_factory=list)
    investigation_ids: list[int] = field(default_factory=list)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _decimal(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(repr(value))


def _impressions(content: ContentItem) -> int:
    value = (content.metrics or {}).get("impressions")
    return int(value) if isinstance(value, int | float) and value > 0 else 0


async def request_investigation(
    session: AsyncSession,
    actor: Actor,
    *,
    group: ClaimGroup,
    content: ContentItem,
    level: str,
    requested_by: str,
    flags: Mapping[str, Any] | None = None,
) -> Investigation:
    """New investigation of ``group`` (stage framing) and its ``investigation.requested``."""
    depth = initial_depth(level)
    investigation = Investigation(
        public_id=uuid.uuid4(),
        claim_group_id=group.id,
        content_id=content.id,
        level=level,
        depth=depth.value,
        stage=FRAMING,
        requested_by=requested_by,
        flags=dict(flags or {}),
    )
    session.add(investigation)
    await session.flush()
    await publish(
        session,
        names.INVESTIGATION_REQUESTED,
        {
            "investigation_id": str(investigation.public_id),
            "group_id": str(group.public_id),
            "level": level,
            "depth": depth.value,
            "content_id": str(content.id),
        },
        aggregate_type="investigation",
        aggregate_id=str(investigation.public_id),
        actor=actor.label,
    )
    return investigation


async def record_extraction(
    session: AsyncSession,
    actor: Actor,
    *,
    content: ContentItem,
    claims: Sequence[ExtractedClaim],
    level: str,
    requested_by: str,
    prompt_version: str,
    thread_context: str | None,
    absence_reason: str | None = None,
    language_not_processed: bool = False,
) -> ExtractionRecord:
    """Store the claims of ``content`` with one group per claim and one investigation per
    verifiable claim; publish ``claim.extracted`` and ``investigation.requested``.

    No claim (absence reason only) writes nothing."""
    actor.require(Permission.FACTCHECK_REQUEST)
    record = ExtractionRecord()
    if not claims:
        log.info("factcheck.no_claim", content=content.id, reason=absence_reason)
        return record

    flags = {"language_not_processed": True} if language_not_processed else {}
    claim_rows: list[Claim] = []
    group_rows: list[ClaimGroup] = []
    investigations: list[tuple[ClaimGroup, Investigation]] = []
    for position, extracted in enumerate(claims):
        sig = extracted.signature
        claim = Claim(
            public_id=uuid.uuid4(),
            lang_original=content.lang,
            text_original=extracted.text_original,
            text_fr=extracted.text_fr,
            claim_type=extracted.claim_type.value,
            subject=sig.subject,
            subject_qid=sig.subject_qid,
            predicate=sig.predicate,
            value_low=_decimal(sig.value_low),
            value_high=_decimal(sig.value_high),
            unit=sig.unit,
            unit_scale=sig.unit_scale,
            currency=sig.currency,
            value_date=_parse_date(sig.value_date),
            period_start=_parse_date(sig.period_start),
            period_end=_parse_date(sig.period_end),
            place=sig.place,
            polarity=sig.polarity.value,
            invoked_source=sig.invoked_source,
            definition_variant=sig.definition_variant,
            comparison_base=sig.comparison_base,
            jurisdictions=list(extracted.jurisdictions),
            primary_jurisdiction=extracted.primary_jurisdiction,
            entities=[e.model_dump(mode="json") for e in extracted.entities],
            theme=extracted.theme,
            verifiability=extracted.verifiability,
            context_hints=[h.value for h in extracted.context_hints],
            source_modality=extracted.source_modality,
            thread_context=thread_context,
            prompt_version=prompt_version,
        )
        session.add(claim)
        await session.flush()
        session.add(ContentClaim(content_id=content.id, claim_id=claim.id, position=position))
        group = ClaimGroup(
            public_id=uuid.uuid4(),
            representative_claim_id=claim.id,
            level=level,
            state=ClaimGroupState.QUEUED.value,
            reach=_impressions(content),
        )
        session.add(group)
        await session.flush()
        session.add(ClaimGroupMember(group_id=group.id, claim_id=claim.id))
        claim_rows.append(claim)
        group_rows.append(group)
        if ClaimType(claim.claim_type) not in UNVERIFIABLE_CLAIM_TYPES:
            investigation = await request_investigation(
                session,
                actor,
                group=group,
                content=content,
                level=level,
                requested_by=requested_by,
                flags=flags,
            )
            investigations.append((group, investigation))
            record.investigation_ids.append(investigation.id)
        record.claim_ids.append(claim.id)
        record.group_ids.append(group.id)

    await publish(
        session,
        names.CLAIM_EXTRACTED,
        {
            "content_id": str(content.id),
            "content_public_id": str(content.public_id),
            "platform": content.platform,
            "external_id": content.external_id,
            "level": level,
            "claim_ids": [str(c.public_id) for c in claim_rows],
            "group_ids": [str(g.public_id) for g in group_rows],
        },
        aggregate_type="content",
        aggregate_id=str(content.public_id),
        actor=actor.label,
    )
    return record


async def promote_to_n4(
    session: AsyncSession,
    actor: Actor,
    *,
    group: ClaimGroup,
    content: ContentItem,
    requested_by: str,
    language_not_processed: bool = False,
) -> Investigation | None:
    """Group → level N4. Starts a reinforced investigation unless the group already has
    an N4 one that did not fail. Returns the new investigation, if any."""
    actor.require(Permission.FACTCHECK_REQUEST)
    group.level = "N4"
    group.updated_at = func.now()
    claim_type = await session.scalar(
        select(Claim.claim_type).where(Claim.id == group.representative_claim_id)
    )
    if claim_type is not None and ClaimType(claim_type) in UNVERIFIABLE_CLAIM_TYPES:
        return None
    existing = await session.scalar(
        select(Investigation.id)
        .where(
            Investigation.claim_group_id == group.id,
            Investigation.level == "N4",
            Investigation.stage != FAILED,
        )
        .limit(1)
    )
    if existing is not None:
        return None
    return await request_investigation(
        session,
        actor,
        group=group,
        content=content,
        level="N4",
        requested_by=requested_by,
        flags={"language_not_processed": True} if language_not_processed else None,
    )
