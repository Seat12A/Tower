"""Source independence (pipeline.md E9, Q11): counted by origin, not by domain."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from tower.core.domain.factcheck.values import ContextNeed, Veracity

__all__ = [
    "IndependenceResult",
    "SourceRef",
    "apply_independence_rule",
    "assess_independence",
    "normalize_origin",
]

#: Two passages whose SimHash differ by at most this many bits are one source.
NEAR_DUPLICATE_BITS = 3


@dataclass(frozen=True, slots=True)
class SourceRef:
    """One cited passage, as seen by the independence rule."""

    passage_id: str
    origin: str | None
    """Entity the information is attributed to (by the judge); ``None`` → domain fallback."""
    domain: str
    """Registrable domain of the source (``insee.fr``)."""
    tier: str
    """``primary``, ``secondary``, ``tertiary`` or ``avoid``."""
    simhash: int
    stakeholder: bool = False
    """Publisher (or its controller) is a party to the claim (lot 6: registry join)."""


@dataclass(frozen=True, slots=True)
class IndependenceResult:
    independent_origins: int
    has_primary: bool
    origins: tuple[str, ...]
    """Normalized origin keys, in first-seen order."""


def normalize_origin(origin: str | None, domain: str) -> str:
    """Comparable key: casefolded origin with collapsed spaces and no surrounding
    punctuation; the domain (prefixed ``domain:``) when the origin is empty."""
    raise NotImplementedError


def assess_independence(refs: Sequence[SourceRef]) -> IndependenceResult:
    """Count independent origins. Rules: ``avoid`` tier and stakeholder sources do not
    count; passages with the same normalized origin count once; near-identical passages
    (SimHash distance ≤ ``NEAR_DUPLICATE_BITS``) count once, as the origin first seen;
    ``has_primary`` when one counted origin has a primary passage."""
    raise NotImplementedError


def apply_independence_rule(
    veracity: Veracity,
    context_need: ContextNeed,
    result: IndependenceResult,
    *,
    has_partial_evidence: bool,
) -> tuple[Veracity, ContextNeed, bool]:
    """``TRUE``/``FALSE`` without two independent origins including a primary one is
    downgraded to ``UNVERIFIABLE``; the context need becomes at least ``USEFUL`` when
    partial evidence exists. Returns ``(veracity, context_need, downgraded)``.
    Other outcomes are returned unchanged."""
    raise NotImplementedError
