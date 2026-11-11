"""Source independence (pipeline.md E9, Q11): counted by origin, not by domain."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from tower.core.domain.factcheck.values import ContextNeed, Veracity
from tower.core.domain.text import hamming

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
    cleaned = _SPACES.sub(" ", (origin or "").casefold()).strip()
    cleaned = _EDGE_PUNCT.sub("", cleaned).strip()
    if not cleaned:
        return f"domain:{domain}"
    return cleaned


def assess_independence(refs: Sequence[SourceRef]) -> IndependenceResult:
    """Count independent origins. Rules: ``avoid`` tier and stakeholder sources do not
    count; passages with the same normalized origin count once; near-identical passages
    (SimHash distance ≤ ``NEAR_DUPLICATE_BITS``) count once, as the origin first seen;
    ``has_primary`` when one counted origin has a primary passage."""
    counted: list[tuple[int, str]] = []  # (simhash, origin key) of every counted passage
    origins: list[str] = []
    primary: set[str] = set()
    for ref in refs:
        if ref.tier == "avoid" or ref.stakeholder:
            continue
        key = normalize_origin(ref.origin, ref.domain)
        for simhash, first_key in counted:
            if hamming(simhash, ref.simhash) <= NEAR_DUPLICATE_BITS:
                key = first_key  # near-duplicate: merged into the origin first seen
                break
        else:
            counted.append((ref.simhash, key))
        if key not in origins:
            origins.append(key)
        if ref.tier == "primary":
            primary.add(key)
    return IndependenceResult(
        independent_origins=len(origins),
        has_primary=bool(primary),
        origins=tuple(origins),
    )


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
    downgraded = False
    if veracity in (Veracity.TRUE, Veracity.FALSE) and not (
        result.independent_origins >= 2 and result.has_primary
    ):
        veracity = Veracity.UNVERIFIABLE
        downgraded = True
    if has_partial_evidence and context_need is ContextNeed.NONE:
        context_need = ContextNeed.USEFUL
    return veracity, context_need, downgraded


_SPACES = re.compile(r"\s+")
_EDGE_PUNCT = re.compile(r"^[\W_]+|[\W_]+$")
