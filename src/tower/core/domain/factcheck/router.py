"""E1 router model: pure-Python logistic regression (scoring, training, evaluation).

Standard library only. Vectors are plain lists of floats (512 values in production);
training runs outside the production replicas (``tower factcheck train-router``) and the
weights are stored in ``router_models``. See docs/factcheck/pipeline.md (E1).
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Callable, Hashable, Sequence

__all__ = [
    "MIN_RECALL_TO_ACTIVATE",
    "evaluate",
    "score",
    "stratified_split",
    "train_logistic",
]

MIN_RECALL_TO_ACTIVATE = 0.90
"""Held-out recall required to activate a router model (evaluation.md, E0-E1)."""


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-min(z, 700.0)))
    e = math.exp(max(z, -700.0))
    return e / (1.0 + e)


def _dot(weights: Sequence[float], vector: Sequence[float]) -> float:
    return math.fsum(w * x for w, x in zip(weights, vector, strict=True))


def score(weights: Sequence[float], bias: float, vector: Sequence[float]) -> float:
    """Probability ``sigmoid(w . x + b)``; raises ``ValueError`` on a size mismatch."""
    if len(weights) != len(vector):
        raise ValueError(f"vector has {len(vector)} values, model expects {len(weights)}")
    return _sigmoid(_dot(weights, vector) + bias)


def train_logistic(
    samples: Sequence[tuple[Sequence[float], int]],
    *,
    sample_weights: Sequence[float] | None = None,
    epochs: int = 30,
    learning_rate: float = 0.1,
    l2: float = 1e-4,
    seed: int = 0,
) -> tuple[list[float], float]:
    """Plain SGD logistic regression, deterministic for a seed.

    Without ``sample_weights`` the classes are balanced (each class weighs half of the
    total). Returns ``(weights, bias)``.
    """
    if not samples:
        raise ValueError("no training samples")
    n = len(samples)
    dims = len(samples[0][0])
    if any(len(x) != dims for x, _ in samples):
        raise ValueError("inconsistent vector sizes")
    if sample_weights is not None:
        if len(sample_weights) != n:
            raise ValueError("sample_weights must match samples")
        weights_of = list(sample_weights)
    else:
        pos = sum(1 for _, y in samples if y == 1)
        neg = n - pos
        w_pos = n / (2 * pos) if pos else 1.0
        w_neg = n / (2 * neg) if neg else 1.0
        weights_of = [w_pos if y == 1 else w_neg for _, y in samples]

    rng = random.Random(seed)  # noqa: S311 (deterministic shuffling, not security)
    w = [0.0] * dims
    b = 0.0
    order = list(range(n))
    for epoch in range(max(1, epochs)):
        rng.shuffle(order)
        lr = learning_rate / (1.0 + 0.05 * epoch)
        for i in order:
            x, y = samples[i]
            err = (_sigmoid(_dot(w, x) + b) - y) * weights_of[i]
            decay = 1.0 - lr * l2
            for j in range(dims):
                w[j] = w[j] * decay - lr * err * x[j]
            b -= lr * err
    return w, b


def _auc(scored: Sequence[tuple[float, int]]) -> float:
    """Rank-based AUC (Mann-Whitney) with average ranks for ties; 0.5 if one class only."""
    pos = sum(1 for _, y in scored if y == 1)
    neg = len(scored) - pos
    if pos == 0 or neg == 0:
        return 0.5
    ordered = sorted(scored, key=lambda item: item[0])
    rank_sum = 0.0
    i = 0
    while i < len(ordered):
        j = i
        while j + 1 < len(ordered) and ordered[j + 1][0] == ordered[i][0]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        rank_sum += avg_rank * sum(1 for k in range(i, j + 1) if ordered[k][1] == 1)
        i = j + 1
    return (rank_sum - pos * (pos + 1) / 2) / (pos * neg)


def evaluate(
    weights: Sequence[float],
    bias: float,
    samples: Sequence[tuple[Sequence[float], int]],
    *,
    threshold: float,
) -> dict[str, float]:
    """Recall, precision, accuracy, AUC and class counts at ``threshold`` (score >= t)."""
    scored = [(score(weights, bias, x), y) for x, y in samples]
    tp = sum(1 for s, y in scored if s >= threshold and y == 1)
    fp = sum(1 for s, y in scored if s >= threshold and y != 1)
    fn = sum(1 for s, y in scored if s < threshold and y == 1)
    tn = len(scored) - tp - fp - fn
    n_pos = tp + fn
    return {
        "recall": tp / n_pos if n_pos else 0.0,
        "precision": tp / (tp + fp) if tp + fp else 0.0,
        "accuracy": (tp + tn) / len(scored) if scored else 0.0,
        "auc": _auc(scored),
        "n_pos": float(n_pos),
        "n_neg": float(len(scored) - n_pos),
    }


def stratified_split[T](
    items: Sequence[T],
    key: Callable[[T], Hashable],
    *,
    holdout: float,
    per_stratum_cap: int | None,
    seed: int,
) -> tuple[list[T], list[T]]:
    """``(train, holdout)`` with the same ``holdout`` fraction inside every stratum.

    Each stratum is shuffled (seeded) and capped at ``per_stratum_cap`` items first.
    A stratum of at least two items keeps at least one item on each side.
    """
    if not 0 <= holdout < 1:
        raise ValueError("holdout must be in [0, 1)")
    strata: dict[Hashable, list[T]] = defaultdict(list)
    for item in items:
        strata[key(item)].append(item)
    rng = random.Random(seed)  # noqa: S311 (deterministic shuffling, not security)
    train: list[T] = []
    held: list[T] = []
    for stratum in sorted(strata, key=repr):
        group = strata[stratum]
        rng.shuffle(group)
        if per_stratum_cap is not None:
            group = group[:per_stratum_cap]
        n_hold = round(len(group) * holdout)
        if holdout > 0 and len(group) >= 2:
            n_hold = min(max(n_hold, 1), len(group) - 1)
        held.extend(group[:n_hold])
        train.extend(group[n_hold:])
    return train, held
