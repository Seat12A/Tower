"""E2 micro-batches (lot 5): composition rules, readiness, canary and solo audit.

Pure: standard library only. A batch mixes posts that cannot contaminate each other: no
two similar posts, no two posts of one narrative. When in doubt a post goes alone.
"""

from __future__ import annotations

import math
import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from tower.core.domain.factcheck.vectors import cosine

__all__ = [
    "BATCH_HARD_CAP",
    "CANARY_PREFIX",
    "INJECTION_MARKERS",
    "BatchCandidate",
    "audit_positions",
    "canary_contaminated",
    "canary_post",
    "compose",
    "estimate_tokens",
    "has_injection_marker",
    "ready",
]

BATCH_HARD_CAP = 12
CANARY_PREFIX = "canary-"

INJECTION_MARKERS: tuple[str, ...] = (
    "ignore previous instructions",
    "ignore all previous",
    "ignore the previous",
    "ignore the above",
    "disregard previous",
    "disregard the above",
    "ignore les instructions",
    "ignore toutes les instructions",
    "ignorez les instructions",
    "ignore tes instructions",
    "oublie les instructions",
    "oublie tes instructions",
    "system prompt",
    "prompt système",
    "prompt systeme",
    "developer message",
    "message du développeur",
    "you are now",
    "tu es maintenant",
    "vous êtes maintenant",
    "act as ",
    "new instructions",
    "nouvelles instructions",
    "</input>",
    "<input",
    "</post",
    "<post",
    "assistant:",
    "system:",
    "[inst]",
    "<|im_start|>",
    "```json",
)
"""Casefolded phrases of known prompt-injection attempts (English and French)."""

_SPACES = re.compile(r"\s+")
_SEPARATORS = re.compile(r"[\s  .,]")


def has_injection_marker(text: str) -> bool:
    """True when ``text`` contains a known injection phrase (casefolded, spaces collapsed)."""
    folded = _SPACES.sub(" ", text.casefold())
    return any(marker in folded for marker in INJECTION_MARKERS)


def estimate_tokens(text: str) -> int:
    """Cheap upper-ish estimate of the token count: ``ceil(len / 3)``."""
    return math.ceil(len(text) / 3)


@dataclass(frozen=True, slots=True)
class BatchCandidate:
    content_id: int
    tokens: int
    vector: Sequence[float] | None
    narrative_id: int | None = None
    solo: bool = False


def _similar(a: BatchCandidate, b: BatchCandidate, dissimilarity: float) -> bool:
    if a.vector is None or b.vector is None or len(a.vector) != len(b.vector):
        return True  # unknown: do not mix
    return cosine(a.vector, b.vector) > dissimilarity


def compose(
    candidates: Sequence[BatchCandidate],
    *,
    size: int,
    max_tokens: int,
    dissimilarity: float,
) -> list[list[int]]:
    """Greedy composition, candidates in the given order (oldest first).

    Each candidate joins the first open batch where it fits: fewer than ``size`` posts
    (never more than 12), token total within ``max_tokens``, no post with cosine above
    ``dissimilarity`` and no post of the same narrative. ``solo`` candidates, posts above
    ``max_tokens`` and posts without a vector go alone. Deterministic.
    """
    cap = max(1, min(size, BATCH_HARD_CAP))
    batches: list[list[BatchCandidate]] = []
    totals: list[int] = []
    for cand in candidates:
        alone = cand.solo or cand.tokens > max_tokens or cand.vector is None
        if alone:
            batches.append([cand])
            totals.append(max_tokens + 1)  # closed: nothing else joins
            continue
        placed = False
        for i, batch in enumerate(batches):
            if len(batch) >= cap or totals[i] + cand.tokens > max_tokens:
                continue
            if any(
                _similar(cand, other, dissimilarity)
                or (cand.narrative_id is not None and cand.narrative_id == other.narrative_id)
                for other in batch
            ):
                continue
            batch.append(cand)
            totals[i] += cand.tokens
            placed = True
            break
        if not placed:
            batches.append([cand])
            totals.append(cand.tokens)
    return [[c.content_id for c in batch] for batch in batches]


def ready(batch_ids: Sequence[int], *, oldest_age_s: float, size: int, max_wait_s: float) -> bool:
    """A batch is sent when full or when its oldest post waited ``max_wait_s``."""
    if not batch_ids:
        return False
    return len(batch_ids) >= min(size, BATCH_HARD_CAP) or oldest_age_s >= max_wait_s


def canary_post(seed: int) -> tuple[str, str]:
    """Synthetic French post with a unique six-digit figure: ``(text, token)``."""
    token = str(random.Random(seed).randint(100_000, 999_999))  # noqa: S311 (not security)
    text = f"La mairie annonce {token} visiteurs dans le musée municipal cette année."
    return text, token


def canary_contaminated(token: str, claims_by_post: Mapping[str, Sequence[str]]) -> bool:
    """True when the canary figure appears in a claim of another post (the canary's own
    entry, keyed ``canary-…``, is ignored). Separators are removed before looking."""
    for post_id, claims in claims_by_post.items():
        if post_id.startswith(CANARY_PREFIX):
            continue
        if any(token in _SEPARATORS.sub("", claim) for claim in claims):
            return True
    return False


class _Rng(Protocol):
    def random(self) -> float: ...


def audit_positions(batch_size: int, rate: float, rng: _Rng) -> list[int]:
    """Positions to replay alone: each position is drawn independently with probability
    ``rate`` (stratified by position: no position is favoured)."""
    return [i for i in range(batch_size) if rng.random() < rate]
