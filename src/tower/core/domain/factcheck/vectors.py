"""Pure-Python vector helpers (cosine, medoid). Standard library only."""

from __future__ import annotations

import math
from collections.abc import Sequence

__all__ = ["cosine", "medoid"]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two vectors; 0.0 when either is a zero vector."""
    if len(a) != len(b):
        raise ValueError("vectors must have the same length")
    dot = math.fsum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(math.fsum(x * x for x in a))
    nb = math.sqrt(math.fsum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / (na * nb)))


def medoid(vectors: Sequence[Sequence[float]]) -> int:
    """Index of the vector minimizing the sum of cosine distances to all others.

    Ties go to the lowest index. Raises ``ValueError`` on an empty input.
    """
    if not vectors:
        raise ValueError("medoid of an empty set")
    if len(vectors) == 1:
        return 0
    best_index = 0
    best_total = math.inf
    for i, vi in enumerate(vectors):
        total = math.fsum(1.0 - cosine(vi, vj) for j, vj in enumerate(vectors) if j != i)
        if total < best_total - 1e-12:
            best_total = total
            best_index = i
    return best_index
