"""Confidence signals of an investigation (pipeline.md E10), from stored outputs.
Shared by the escalation step (provisional confidence), the assembly (final) and the
evaluation tool."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from tower.core.domain.factcheck.confidence import ConfidenceSignals
from tower.core.domain.factcheck.independence import IndependenceResult, SourceRef
from tower.core.domain.factcheck.values import Agreement, ClaimType, ContextType, Veracity

__all__ = ["build_signals"]


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
