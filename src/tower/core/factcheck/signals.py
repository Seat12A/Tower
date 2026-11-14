"""Confidence signals of an investigation (pipeline.md E10), from stored outputs.
Shared by the escalation step (provisional confidence), the assembly (final) and the
evaluation tool."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

from tower.core.domain.factcheck.confidence import ConfidenceSignals
from tower.core.domain.factcheck.independence import (
    IndependenceResult,
    SourceRef,
    assess_independence,
)
from tower.core.domain.factcheck.values import Agreement, ClaimType, ContextType, Veracity

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from tower.core.models.factcheck import Investigation, Verdict

__all__ = ["build_signals", "stored_signals"]


def build_signals(
    *,
    veracity: Veracity,
    claim_type: str,
    pitfalls: Iterable[str],
    context_types: Iterable[str],
    cited: Sequence[SourceRef],
    primary_passage_ids: Iterable[str],
    independence: IndependenceResult,
    conflicting_sources: bool,
    agreement: Agreement | None,
    second_pass: bool | None,
    coverage_b: bool = False,
) -> ConfidenceSignals:
    """``primary_passage_ids``: cited passages the judge marked as primary. A primary
    direct source = one of them comes from a ``primary`` registry tier (an avoid-tier or
    stakeholder source never counts)."""
    primary_ids = set(primary_passage_ids)
    primary_direct = any(
        r.passage_id in primary_ids and r.tier == "primary" and not r.stakeholder for r in cited
    )
    pitfall_set = set(pitfalls)
    contexts = set(context_types)
    return ConfidenceSignals(
        primary_direct=primary_direct,
        independent_origins=independence.independent_origins,
        conflicting_sources=conflicting_sources,
        agreement=agreement,
        second_pass=second_pass,
        statistic_with_reference=False,  # reference facts arrive with lot 4
        ongoing_situation=claim_type == ClaimType.ONGOING_SITUATION
        or ContextType.DEVELOPING in contexts,
        causation=claim_type == ClaimType.CAUSAL or ContextType.CAUSATION in pitfall_set,
        legal_interpretation=claim_type == ClaimType.LEGAL_TEXT,
        outcome_misleading=veracity is Veracity.MISLEADING,
        coverage_b=coverage_b,
    )


async def stored_signals(
    session: AsyncSession,
    verdict: Verdict,
    investigation: Investigation,
    claim_type: str,
    *,
    veracity: Veracity | None = None,
) -> ConfidenceSignals:
    """Signals of a stored draft from its valid citations and flags (assembly, and the
    offline recalibration). ``veracity`` overrides the stored one (the outcome the model
    proposed, before a reviewer edit)."""
    from sqlalchemy import select

    from tower.core.models.factcheck import Citation, EvidencePassage, Source

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
            stakeholder=r.label in set(flags.get("stakeholder_passages") or []),
        )
        for r in rows
    ]
    independence = assess_independence(refs)
    second_pass = {"concordant": True, "discordant": False}.get(str(flags.get("second_pass")))
    agreement_value = flags.get("agreement") or investigation.agreement
    return build_signals(
        veracity=veracity or Veracity(verdict.veracity),
        claim_type=claim_type,
        pitfalls=(investigation.framing or {}).get("known_pitfalls") or [],
        context_types=verdict.context_types or [],
        cited=refs,
        primary_passage_ids=flags.get("primary_passages") or [],
        independence=independence,
        conflicting_sources=bool(flags.get("conflicting_sources")),
        agreement=Agreement(agreement_value) if agreement_value else None,
        second_pass=second_pass,
    )
