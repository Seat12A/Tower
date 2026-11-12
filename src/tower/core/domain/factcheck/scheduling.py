"""Pure rules of the E4 scheduler: pass interval, reach, priority, promotion and demotion
conditions (docs/factcheck/niveaux.md). Standard library only.

Nothing here reads the author of a post, its account or any political side: the inputs
are the observations of the group, the platform metrics, the theme, the verifiability
and the narrative wave state.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from tower.core.domain.factcheck.values import Band, LightSignal

__all__ = [
    "METRIC_REASONS",
    "REASON_LIGHT_SIGNAL",
    "REASON_NARRATIVE",
    "REASON_OBSERVERS",
    "REASON_VELOCITY",
    "REASON_VIEWS",
    "GroupMetrics",
    "PriorityInput",
    "PromotionThresholds",
    "demotion_due",
    "interval_s",
    "light_signal_from_band",
    "priority",
    "promotion_confirmed",
    "promotion_reasons",
    "reach",
    "theme_risk",
    "wave_factor",
]

REASON_OBSERVERS = "observers"
REASON_VELOCITY = "velocity"
REASON_VIEWS = "views"
REASON_LIGHT_SIGNAL = "light_signal"
REASON_NARRATIVE = "narrative"
METRIC_REASONS = frozenset({REASON_VIEWS})
"""Reasons that rest on platform metrics, so that a fresh platform read can contradict
them. The others come from our own observations, signals or narratives."""

_HIGH_RISK_THEMES = frozenset({"health", "elections", "security", "disaster"})
_MID_RISK_THEMES = frozenset({"economy", "justice", "immigration", "international"})
_WAVE_FACTORS = {"watch": 2.0, "surge": 3.0, "alert": 5.0}
_SURGE_OR_ABOVE = frozenset({"surge", "alert"})
_NOVELTY_NEW = 1.0
_NOVELTY_REVALIDATION = 0.3
_COST_FLOOR_CENTS = 1.0


def interval_s(rate_per_hour: float, *, target: float, min_s: int, max_s: int) -> int:
    """Seconds between two scheduler passes: ``clamp(min, max, 3600 x target / rate)``
    with ``rate`` the new contents of the last hour (a quiet flow waits the maximum)."""
    if rate_per_hour <= 0:
        return max_s
    raw = 3600.0 * target / rate_per_hour
    return int(min(max_s, max(min_s, round(raw))))


def reach(views: float, observers: float, velocity: float) -> float:
    """``log1p(views) + 2 log1p(observers) + velocity`` (negative inputs count as 0)."""
    return (
        math.log1p(max(views, 0.0))
        + 2.0 * math.log1p(max(observers, 0.0))
        + max(velocity, 0.0)
    )


def theme_risk(theme: str | None) -> float:
    """3 for health, elections, security and disaster; 2 for economy, justice, immigration
    and international; 1 otherwise."""
    key = (theme or "").strip().casefold()
    if key in _HIGH_RISK_THEMES:
        return 3.0
    if key in _MID_RISK_THEMES:
        return 2.0
    return 1.0


def wave_factor(narrative_state: str | None) -> float:
    """1 (no narrative or ``active``), 2 (watch), 3 (surge), 5 (alert)."""
    return _WAVE_FACTORS.get(narrative_state or "", 1.0)


@dataclass(frozen=True, slots=True)
class PriorityInput:
    views: float
    observers: float
    velocity: float
    theme: str | None
    verifiability: float
    revalidation: bool = False
    narrative_state: str | None = None
    level_cost_cents: float = _COST_FLOOR_CENTS
    """Mean cost of the level's investigations over 30 days, in cents (floored at 1)."""


def priority(data: PriorityInput) -> float:
    """``reach x theme_risk x verifiability x novelty x wave_factor / cost(level)``."""
    novelty = _NOVELTY_REVALIDATION if data.revalidation else _NOVELTY_NEW
    cost = max(data.level_cost_cents, _COST_FLOOR_CENTS)
    verifiability = min(max(data.verifiability, 0.0), 1.0)
    return (
        reach(data.views, data.observers, data.velocity)
        * theme_risk(data.theme)
        * verifiability
        * novelty
        * wave_factor(data.narrative_state)
        / cost
    )


@dataclass(frozen=True, slots=True)
class GroupMetrics:
    observers_6h: int
    velocity: float
    """Observations of the last hour / the previous hour; 0 under 10 observations."""
    views: int
    reach: float
    light_signal: str | None = None
    narrative_state: str | None = None


@dataclass(frozen=True, slots=True)
class PromotionThresholds:
    observers: int
    velocity_factor: float
    views: int
    median_reach: float
    """Median ``reach`` of the active N1 groups (light signal condition)."""


def promotion_reasons(metrics: GroupMetrics, thresholds: PromotionThresholds) -> list[str]:
    """Entry conditions of N2 that hold (one suffices). Empty: no promotion."""
    reasons: list[str] = []
    if metrics.observers_6h >= thresholds.observers:
        reasons.append(REASON_OBSERVERS)
    if metrics.velocity >= thresholds.velocity_factor:
        reasons.append(REASON_VELOCITY)
    if metrics.views > thresholds.views:
        reasons.append(REASON_VIEWS)
    if (
        metrics.light_signal == LightSignal.LIKELY_FALSE.value
        and metrics.reach > thresholds.median_reach
    ):
        reasons.append(REASON_LIGHT_SIGNAL)
    if metrics.narrative_state in _SURGE_OR_ABOVE:
        reasons.append(REASON_NARRATIVE)
    return reasons


def promotion_confirmed(reasons: list[str], fresh_views: int, thresholds: PromotionThresholds) -> bool:
    """A promotion stands when one reason is not metric-based, or when the fresh platform
    metrics still exceed the views threshold."""
    if not reasons:
        return False
    if any(reason not in METRIC_REASONS for reason in reasons):
        return True
    return fresh_views > thresholds.views


def demotion_due(
    below_since: datetime | None, *, now: datetime, hours: int, investigating: bool
) -> bool:
    """N2 -> N1 once below every entry condition for ``hours``, never while an
    investigation is running."""
    if investigating or below_since is None:
        return False
    return now - below_since >= timedelta(hours=hours)


def light_signal_from_band(band: Band | str) -> LightSignal:
    """Deterministic E5 signal of a numeric claim against a validated fact: ``contains``
    -> likely true, ``within_1_tau`` -> needs context, any other band -> likely false."""
    value = band.value if isinstance(band, Band) else str(band)
    if value == Band.CONTAINS.value:
        return LightSignal.LIKELY_TRUE
    if value == Band.WITHIN_1_TAU.value:
        return LightSignal.NEEDS_CONTEXT
    return LightSignal.LIKELY_FALSE
