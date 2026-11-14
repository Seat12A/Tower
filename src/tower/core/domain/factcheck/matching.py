"""Claim-to-group matching plan (E3): decide what to do before any arbiter call."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from tower.core.domain.factcheck.signatures import SignatureComparison
from tower.core.domain.factcheck.values import Band, BandSide, MatchOutcome

__all__ = [
    "MAX_ARBITER_CALLS",
    "Candidate",
    "MatchDecision",
    "MatchPlan",
    "plan_match",
]

MAX_ARBITER_CALLS = 2


@dataclass(frozen=True, slots=True)
class Candidate:
    group_id: int
    claim_lang: str  # language of the group's representative claim
    similarity_pivot: float  # cosine in pivot space
    similarity_original: float | None  # cosine of original vectors, same language only
    comparison: SignatureComparison
    has_active_verdict: bool


@dataclass(frozen=True, slots=True)
class MatchPlan:
    join_without_arbiter: int | None
    arbiter_queue: tuple[int, ...]
    related_candidates: tuple[int, ...]
    rejected: Mapping[int, str]


def _same_language(claim_lang: str, candidate: Candidate) -> bool:
    return claim_lang.casefold() == candidate.claim_lang.casefold()


def _similarity(claim_lang: str, candidate: Candidate) -> float:
    if _same_language(claim_lang, candidate) and candidate.similarity_original is not None:
        return candidate.similarity_original
    return candidate.similarity_pivot


def plan_match(
    claim_lang: str,
    candidates: Sequence[Candidate],
    *,
    same_threshold: float,
    grey_threshold: float,
) -> MatchPlan:
    """Sort candidates and split them into join / arbiter / related / rejected.

    Safety first: a same-language candidate without an original-vector similarity can
    never be joined without the arbiter.
    """
    ordered = sorted(
        candidates,
        key=lambda c: (-_similarity(claim_lang, c), not c.has_active_verdict, c.group_id),
    )
    join: int | None = None
    arbiter: list[int] = []
    related: list[int] = []
    rejected: dict[int, str] = {}

    for cand in ordered:
        gid = cand.group_id
        comparison = cand.comparison
        if comparison.conflicting:
            names = ",".join(sorted(d.value for d in comparison.differences))
            rejected[gid] = f"signature:{names}"
            continue
        same_lang = _same_language(claim_lang, cand)
        sim = _similarity(claim_lang, cand)
        if sim < grey_threshold:
            rejected[gid] = "below_grey"
            continue
        if not comparison.identical:
            # Only MISSING differences: never joined, one arbiter call at most.
            if related:
                rejected[gid] = "related_cap"
            else:
                related.append(gid)
            continue
        if same_lang and cand.similarity_original is not None and sim >= same_threshold:
            if join is None:
                join = gid
            else:
                rejected[gid] = "join_taken"
            continue
        if len(arbiter) < MAX_ARBITER_CALLS:
            arbiter.append(gid)
        else:
            rejected[gid] = "arbiter_cap"

    if join is not None:
        for gid in arbiter:
            rejected[gid] = "join_taken"
        arbiter = []
    return MatchPlan(
        join_without_arbiter=join,
        arbiter_queue=tuple(arbiter),
        related_candidates=tuple(related),
        rejected=rejected,
    )


@dataclass(frozen=True, slots=True)
class MatchDecision:
    outcome: MatchOutcome
    group_id: int | None
    rule: str
    similarity: float | None = None
    arbiter_reason: str | None = None
    related_group_id: int | None = None
    embedding: tuple[float, ...] | None = None
    embedding_pivot: tuple[float, ...] | None = None
    embedding_model: str | None = None
    reference_fact_id: int | None = None
    band: Band | None = None
    band_side: BandSide | None = None
    outdated: bool = False
    external_factchecks: tuple[Mapping[str, str], ...] = field(default=())

    @classmethod
    def join(cls, group_id: int, rule: str, **kwargs: object) -> MatchDecision:
        return cls(outcome=MatchOutcome.JOIN, group_id=group_id, rule=rule, **kwargs)  # type: ignore[arg-type]

    @classmethod
    def new(cls, rule: str = "new:no_candidate", **kwargs: object) -> MatchDecision:
        return cls(outcome=MatchOutcome.NEW, group_id=None, rule=rule, **kwargs)  # type: ignore[arg-type]
