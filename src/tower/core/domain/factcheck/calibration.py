"""Recalibration of the draft confidence (pipeline.md E10, feuille-de-route lot 6).

The hand-set coefficients of ``confidence.py`` are refitted on the reviewers' decisions,
never below the data thresholds:

- **reduced** model (from ``REDUCED_MIN_REVIEWS`` reviews): the intercept and
  ``REDUCED_FEATURES`` (4 coefficients) are refitted, shrunk towards their current values;
  every other coefficient stays at its current value;
- **full** model (from ``FULL_MIN_REFUSALS`` refusals): every coefficient is refitted,
  shrunk towards its current value.

A model is only a candidate: it replaces the current one only when its held-out expected
calibration error (ECE) is lower. Pure: standard library only.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from math import exp, log

from tower.core.domain.factcheck.confidence import Coefficients, ConfidenceSignals
from tower.core.domain.factcheck.values import Agreement

__all__ = [
    "FULL_MIN_REFUSALS",
    "REDUCED_FEATURES",
    "REDUCED_MIN_REVIEWS",
    "CalibrationSample",
    "Fit",
    "brier",
    "eligible_kind",
    "expected_calibration_error",
    "fit_model",
    "indicator_features",
    "predict",
    "split_holdout",
]

REDUCED_MIN_REVIEWS = 200
FULL_MIN_REFUSALS = 100
REDUCED_FEATURES = (
    "primary_direct",
    "two_independent_origins",
    "second_opinion_identical",
    "conflicting_sources",
)
"""The 3-4 coefficients of the reduced model (with the intercept)."""

_COEFFICIENT_NAMES = tuple(f.name for f in fields(Coefficients) if f.name != "intercept")


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + exp(-z))
    e = exp(z)
    return e / (1.0 + e)


def indicator_features(signals: ConfidenceSignals) -> dict[str, float]:
    """0/1 value of every coefficient of ``Coefficients`` for these signals, exactly as
    ``linear_score`` adds them (one origin coefficient at most, one agreement at most)."""
    x = dict.fromkeys(_COEFFICIENT_NAMES, 0.0)
    x["primary_direct"] = float(signals.primary_direct)
    if signals.independent_origins >= 3:
        x["three_plus_independent_origins"] = 1.0
    elif signals.independent_origins == 2:
        x["two_independent_origins"] = 1.0
    x["conflicting_sources"] = float(signals.conflicting_sources)
    if signals.agreement is Agreement.IDENTICAL:
        x["second_opinion_identical"] = 1.0
    elif signals.agreement is Agreement.STYLE:
        x["second_opinion_same_side"] = 1.0
    elif signals.agreement is Agreement.SUBSTANTIVE:
        x["second_opinion_substantive"] = 1.0
    if signals.second_pass is True:
        x["second_pass_concordant"] = 1.0
    elif signals.second_pass is False:
        x["second_pass_discordant"] = 1.0
    for name in (
        "statistic_with_reference",
        "ongoing_situation",
        "causation",
        "legal_interpretation",
        "outcome_misleading",
        "coverage_b",
    ):
        x[name] = float(getattr(signals, name))
    return x


@dataclass(frozen=True, slots=True)
class CalibrationSample:
    features: Mapping[str, float]
    label: int
    """1: the reviewers kept the outcome; 0: rejected or outcome changed."""
    refusal: bool = False
    """``reject`` decision (counts towards ``FULL_MIN_REFUSALS``)."""


def eligible_kind(reviews: int, refusals: int) -> str | None:
    """``full`` from ``FULL_MIN_REFUSALS`` refusals (and the review threshold), ``reduced``
    from ``REDUCED_MIN_REVIEWS`` reviews, otherwise ``None`` (nothing may be produced)."""
    if reviews >= REDUCED_MIN_REVIEWS and refusals >= FULL_MIN_REFUSALS:
        return "full"
    if reviews >= REDUCED_MIN_REVIEWS:
        return "reduced"
    return None


def predict(coefficients: Coefficients, features: Mapping[str, float], cap: float = 1.0) -> float:
    z = coefficients.intercept + sum(
        getattr(coefficients, name) * value for name, value in features.items() if value
    )
    return min(_sigmoid(z), cap)


@dataclass(frozen=True, slots=True)
class Fit:
    coefficients: Coefficients
    fitted: tuple[str, ...]
    """Names refitted (``intercept`` included)."""
    iterations: int
    log_loss: float


def fit_model(
    samples: Sequence[CalibrationSample],
    prior: Coefficients,
    *,
    kind: str,
    shrinkage: float = 2.0,
    iterations: int = 2000,
    learning_rate: float = 0.5,
) -> Fit:
    """Penalized logistic regression by full-batch gradient descent (deterministic):
    minimizes the mean log-loss + ``shrinkage / n`` × Σ (w − prior)² over the refitted
    names. Unfitted names keep their prior value (an offset)."""
    if not samples:
        raise ValueError("no samples")
    names = ("intercept", *(REDUCED_FEATURES if kind == "reduced" else _COEFFICIENT_NAMES))
    w = {name: float(getattr(prior, name)) for name in ("intercept", *_COEFFICIENT_NAMES)}
    n = len(samples)
    penalty = shrinkage / n
    loss = 0.0
    step = 0
    for step in range(1, iterations + 1):  # noqa: B007 (reported)
        grad = dict.fromkeys(names, 0.0)
        loss = 0.0
        for s in samples:
            z = w["intercept"] + sum(w[k] * v for k, v in s.features.items() if v)
            p = _sigmoid(z)
            p_safe = min(max(p, 1e-12), 1 - 1e-12)
            loss -= s.label * log(p_safe) + (1 - s.label) * log(1 - p_safe)
            err = p - s.label
            grad["intercept"] += err
            for k in names[1:]:
                value = s.features.get(k, 0.0)
                if value:
                    grad[k] += err * value
        loss /= n
        delta = 0.0
        for k in names:
            g = grad[k] / n + 2 * penalty * (w[k] - float(getattr(prior, k)))
            w[k] -= learning_rate * g
            delta = max(delta, abs(learning_rate * g))
        if delta < 1e-7:
            break
    fitted = Coefficients(**{k: round(v, 6) for k, v in w.items()})
    return Fit(coefficients=fitted, fitted=names, iterations=step, log_loss=round(loss, 6))


def expected_calibration_error(
    pairs: Sequence[tuple[float, int]], *, bins: int = 10
) -> float | None:
    """ECE: Σ_b (n_b / n) × |mean predicted − observed rate| over equal-width bins;
    ``None`` without pairs. The lot criterion is a mean gap below 0.1."""
    if not pairs:
        return None
    totals = [[0, 0.0, 0] for _ in range(bins)]
    for p, y in pairs:
        b = min(int(p * bins), bins - 1)
        totals[b][0] += 1
        totals[b][1] += p
        totals[b][2] += y
    n = len(pairs)
    ece = sum(count / n * abs(psum / count - ysum / count) for count, psum, ysum in totals if count)
    return round(ece, 4)


def brier(pairs: Sequence[tuple[float, int]]) -> float | None:
    if not pairs:
        return None
    return round(sum((p - y) ** 2 for p, y in pairs) / len(pairs), 4)


def split_holdout[T](items: Sequence[T], *, holdout: float, seed: int) -> tuple[list[T], list[T]]:
    """Seeded shuffle then ``(train, holdout)``; at least one item on each side when
    there are two or more items."""
    order = list(items)
    random.Random(seed).shuffle(order)  # noqa: S311 (deterministic split, not security)
    n_hold = round(len(order) * holdout)
    if len(order) >= 2:
        n_hold = min(max(n_hold, 1), len(order) - 1)
    return order[n_hold:], order[:n_hold]
