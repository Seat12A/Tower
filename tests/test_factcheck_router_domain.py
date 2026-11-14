"""E1 router domain: scoring, logistic training, evaluation, stratified split (pure)."""

from __future__ import annotations

import math
import random
from collections import Counter

import pytest

from tower.core.domain.factcheck.router import (
    MIN_RECALL_TO_ACTIVATE,
    evaluate,
    score,
    stratified_split,
    train_logistic,
)

DIMS = 8


def synthetic(n: int, seed: int = 1) -> list[tuple[list[float], int]]:
    """Linearly separable: label = sign of the first coordinate (with a margin)."""
    rng = random.Random(seed)  # noqa: S311
    out: list[tuple[list[float], int]] = []
    for i in range(n):
        label = i % 2
        x = [rng.uniform(-1, 1) for _ in range(DIMS)]
        x[0] = rng.uniform(0.3, 1.0) * (1 if label else -1)
        out.append((x, label))
    return out


def test_score_is_a_probability_and_safe_for_large_inputs() -> None:
    assert score([0.0, 0.0], 0.0, [1.0, 2.0]) == 0.5
    assert score([1.0], 0.0, [1e6]) == 1.0
    assert score([1.0], 0.0, [-1e6]) == pytest.approx(0.0, abs=1e-300)
    assert 0 < score([1.0, -1.0], 0.2, [0.3, 0.1]) < 1
    with pytest.raises(ValueError, match=r"size|expects"):
        score([1.0], 0.0, [1.0, 2.0])


def test_training_separates_a_linearly_separable_set() -> None:
    data = synthetic(400)
    train, test = data[:300], data[300:]
    w, b = train_logistic(train, seed=3)
    m = evaluate(w, b, test, threshold=0.5)
    assert m["recall"] >= 0.95 and m["precision"] >= 0.95
    assert m["accuracy"] >= 0.95 and m["auc"] >= 0.99
    assert m["n_pos"] == 50 and m["n_neg"] == 50
    assert w[0] > 0


def test_training_is_deterministic_for_a_seed() -> None:
    data = synthetic(100)
    assert train_logistic(data, seed=5) == train_logistic(data, seed=5)
    assert train_logistic(data, seed=5) != train_logistic(data, seed=6)


def test_class_balancing_keeps_recall_on_an_imbalanced_set() -> None:
    data = [s for s in synthetic(600) if s[1] == 0 or random.Random(s[0][1]).random() < 0.1]  # noqa: S311
    w, b = train_logistic(data, seed=0)
    assert evaluate(w, b, synthetic(100, seed=9), threshold=0.5)["recall"] >= 0.9


def test_explicit_sample_weights_and_errors() -> None:
    data = synthetic(40)
    w, _ = train_logistic(data, sample_weights=[1.0] * 40, seed=0)
    assert len(w) == DIMS
    with pytest.raises(ValueError, match="no training"):
        train_logistic([])
    with pytest.raises(ValueError, match="sample_weights"):
        train_logistic(data, sample_weights=[1.0])


def test_evaluate_known_values_and_auc_ties() -> None:
    # weights [1], bias 0: score = sigmoid(x)
    samples = [([2.0], 1), ([1.0], 1), ([-1.0], 1), ([0.5], 0), ([-2.0], 0), ([-3.0], 0)]
    m = evaluate([1.0], 0.0, samples, threshold=0.5)
    assert m["recall"] == pytest.approx(2 / 3)
    assert m["precision"] == pytest.approx(2 / 3)
    assert m["accuracy"] == pytest.approx(4 / 6)
    assert m["auc"] == pytest.approx(8 / 9)  # 8 of 9 pos/neg pairs ranked correctly
    tied = evaluate([0.0], 0.0, [([1.0], 1), ([1.0], 0)], threshold=0.5)
    assert tied["auc"] == 0.5
    assert evaluate([1.0], 0.0, [([1.0], 1)], threshold=0.5)["auc"] == 0.5


def test_stratified_split_proportions_cap_and_determinism() -> None:
    items = [("a", i) for i in range(100)] + [("b", i) for i in range(40)] + [("c", 0), ("c", 1)]
    train, held = stratified_split(
        items, lambda x: x[0], holdout=0.25, per_stratum_cap=None, seed=1
    )
    held_counts = Counter(k for k, _ in held)
    assert held_counts["a"] == 25 and held_counts["b"] == 10 and held_counts["c"] == 1
    assert len(train) + len(held) == len(items)
    assert not set(train) & set(held)
    again = stratified_split(items, lambda x: x[0], holdout=0.25, per_stratum_cap=None, seed=1)
    assert (train, held) == again

    capped_train, capped_held = stratified_split(
        items, lambda x: x[0], holdout=0.25, per_stratum_cap=20, seed=1
    )
    counts = Counter(k for k, _ in capped_train + capped_held)
    assert counts["a"] == 20 and counts["b"] == 20 and counts["c"] == 2
    with pytest.raises(ValueError, match="holdout"):
        stratified_split(items, lambda x: x[0], holdout=1.0, per_stratum_cap=None, seed=0)


def test_activation_target_constant() -> None:
    assert math.isclose(MIN_RECALL_TO_ACTIVATE, 0.90)
