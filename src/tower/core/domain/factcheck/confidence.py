"""Calibrated confidence of a draft (pipeline.md E10): a logistic regression with
hand-set coefficients, capped before calibration. Coefficients and thresholds are
configuration (``TOWER_FC_CONFIDENCE_*``)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, replace
from enum import StrEnum
from math import exp

from tower.core.domain.factcheck.values import Agreement

__all__ = [
    "Coefficients",
    "ConfidenceBand",
    "ConfidenceSignals",
    "Thresholds",
    "band",
    "compute_confidence",
    "linear_score",
]


@dataclass(frozen=True, slots=True)
class Coefficients:
    """Values of pipeline.md E10 (set by hand, not measured)."""

    intercept: float = 0.0
    primary_direct: float = 1.0
    two_independent_origins: float = 0.5
    three_plus_independent_origins: float = 0.8
    conflicting_sources: float = -1.5
    second_opinion_same_side: float = 0.8
    second_opinion_identical: float = 1.1
    second_opinion_substantive: float = -1.5
    second_pass_concordant: float = 0.4
    second_pass_discordant: float = -1.2
    statistic_with_reference: float = 0.5
    ongoing_situation: float = -0.8
    causation: float = -1.0
    legal_interpretation: float = -0.5
    outcome_misleading: float = -0.4
    coverage_b: float = -0.3

    def with_overrides(self, overrides: Mapping[str, float]) -> Coefficients:
        """Copy with some coefficients replaced; unknown names raise ``ValueError``."""
        known = {f.name for f in fields(self)}
        unknown = sorted(set(overrides) - known)
        if unknown:
            raise ValueError(f"Unknown confidence coefficients: {', '.join(unknown)}")
        return replace(self, **{k: float(v) for k, v in overrides.items()})


@dataclass(frozen=True, slots=True)
class Thresholds:
    cap: float = 0.90
    high: float = 0.80
    medium: float = 0.55


class ConfidenceBand(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True, slots=True)
class ConfidenceSignals:
    primary_direct: bool = False
    """A cited passage from a primary source directly supports the outcome."""
    independent_origins: int = 0
    conflicting_sources: bool = False
    agreement: Agreement | None = None
    """Judge vs second opinion; ``None`` when no second opinion was obtained."""
    second_pass: bool | None = None
    """``True`` concordant, ``False`` discordant, ``None`` no second pass."""
    statistic_with_reference: bool = False
    ongoing_situation: bool = False
    causation: bool = False
    legal_interpretation: bool = False
    outcome_misleading: bool = False
    coverage_b: bool = False


def linear_score(signals: ConfidenceSignals, coefficients: Coefficients) -> float:
    """Sum of the intercept and the coefficients of the active signals. Origins: 2 →
    ``two_independent_origins``, 3 or more → ``three_plus_independent_origins`` (not both).
    Agreement: identical → ``second_opinion_identical``, style → ``second_opinion_same_side``,
    substantive → ``second_opinion_substantive``."""
    c = coefficients
    total = c.intercept
    if signals.primary_direct:
        total += c.primary_direct
    if signals.independent_origins >= 3:
        total += c.three_plus_independent_origins
    elif signals.independent_origins == 2:
        total += c.two_independent_origins
    if signals.conflicting_sources:
        total += c.conflicting_sources
    if signals.agreement is Agreement.IDENTICAL:
        total += c.second_opinion_identical
    elif signals.agreement is Agreement.STYLE:
        total += c.second_opinion_same_side
    elif signals.agreement is Agreement.SUBSTANTIVE:
        total += c.second_opinion_substantive
    if signals.second_pass is True:
        total += c.second_pass_concordant
    elif signals.second_pass is False:
        total += c.second_pass_discordant
    if signals.statistic_with_reference:
        total += c.statistic_with_reference
    if signals.ongoing_situation:
        total += c.ongoing_situation
    if signals.causation:
        total += c.causation
    if signals.legal_interpretation:
        total += c.legal_interpretation
    if signals.outcome_misleading:
        total += c.outcome_misleading
    if signals.coverage_b:
        total += c.coverage_b
    return total


def compute_confidence(
    signals: ConfidenceSignals,
    coefficients: Coefficients | None = None,
    thresholds: Thresholds | None = None,
) -> float:
    """``min(sigmoid(linear_score), thresholds.cap)``, rounded to 4 decimals."""
    score = linear_score(signals, coefficients or Coefficients())
    cap = (thresholds or Thresholds()).cap
    return round(min(_sigmoid(score), cap), 4)


def band(confidence: float, thresholds: Thresholds | None = None) -> ConfidenceBand:
    """HIGH from ``thresholds.high``, MEDIUM from ``thresholds.medium``, LOW below."""
    t = thresholds or Thresholds()
    if confidence >= t.high:
        return ConfidenceBand.HIGH
    if confidence >= t.medium:
        return ConfidenceBand.MEDIUM
    return ConfidenceBand.LOW


def _sigmoid(x: float) -> float:
    """Logistic function, safe for large ``|x|`` (never overflows)."""
    if x >= 0:
        return 1.0 / (1.0 + exp(-x))
    e = exp(x)
    return e / (1.0 + e)
