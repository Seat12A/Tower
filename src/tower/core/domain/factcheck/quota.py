"""Daily draft quota (niveaux.md, "Quota de brouillons"): at most N drafts per UTC day
go to review; when there are more candidates than slots, doubtful cases first, then
``false`` and ``misleading``, then the rest."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from tower.core.domain.factcheck.values import Agreement, Veracity

__all__ = ["DraftCandidate", "is_doubtful", "priority_key", "select_for_review"]


@dataclass(frozen=True, slots=True)
class DraftCandidate:
    key: str
    veracity: Veracity
    confidence: float
    agreement: Agreement | None
    conflicting_sources: bool
    level: str
    reach: int
    """Audience proxy (impressions or followers); 0 when unknown."""
    ready_at: datetime


def is_doubtful(candidate: DraftCandidate, *, medium_threshold: float = 0.80) -> bool:
    """Substantive disagreement, conflicting sources, or confidence below the HIGH band.

    Note: the parameter is named ``medium_threshold`` but it is the threshold of the HIGH
    band (0.80): a draft below it is not confident enough to skip the doubtful queue.
    """
    return (
        candidate.agreement is Agreement.SUBSTANTIVE
        or candidate.conflicting_sources
        or candidate.confidence < medium_threshold
    )


def priority_key(candidate: DraftCandidate) -> tuple[int, int, int, float, datetime]:
    """Sort key (ascending = first): N4 first, then doubtful, then false/misleading, then
    higher reach, then older ``ready_at``. Never uses the author."""
    return (
        0 if candidate.level == "N4" else 1,
        0 if is_doubtful(candidate) else 1,
        0 if candidate.veracity in (Veracity.FALSE, Veracity.MISLEADING) else 1,
        -float(candidate.reach),
        candidate.ready_at,
    )


def select_for_review(candidates: Sequence[DraftCandidate], *, slots: int) -> list[DraftCandidate]:
    """The ``slots`` best candidates by ``priority_key`` (none when ``slots <= 0``)."""
    if slots <= 0:
        return []
    ordered = sorted(candidates, key=lambda c: (priority_key(c), c.key))
    return ordered[:slots]
