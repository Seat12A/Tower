"""Pure rules of the E4 scheduler and the E5 band signal (no database)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from tower.core.domain.factcheck import scheduling as s
from tower.core.domain.factcheck.values import Band, LightSignal

THRESHOLDS = s.PromotionThresholds(observers=5, velocity_factor=3.0, views=50_000, median_reach=4.0)
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


def metrics(**kw: object) -> s.GroupMetrics:
    base: dict[str, object] = {"observers_6h": 0, "velocity": 0.0, "views": 0, "reach": 0.0}
    base.update(kw)
    return s.GroupMetrics(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------- interval


@pytest.mark.parametrize(
    ("rate", "expected"),
    [(0, 1800), (-3, 1800), (10, 1800), (600, 60), (6, 1800), (100, 180), (20_000, 60)],
)
def test_interval_is_clamped(rate: float, expected: int) -> None:
    assert s.interval_s(rate, target=5.0, min_s=60, max_s=1800) == expected


def test_interval_follows_the_rate_between_bounds() -> None:
    assert s.interval_s(36, target=5, min_s=60, max_s=1800) == 500
    assert s.interval_s(100, target=5, min_s=60, max_s=1800) == 180


# ------------------------------------------------------------------- reach


def test_reach_formula_and_negative_inputs() -> None:
    assert s.reach(0, 0, 0) == 0
    assert s.reach(99, 0, 0) == pytest.approx(math.log(100))
    assert s.reach(0, 4, 0) == pytest.approx(2 * math.log(5))
    assert s.reach(0, 0, 2.5) == 2.5
    assert s.reach(-5, -1, -2) == 0


@pytest.mark.parametrize(
    ("theme", "risk"),
    [
        ("health", 3.0),
        ("Elections", 3.0),
        ("security", 3.0),
        ("disaster", 3.0),
        ("economy", 2.0),
        ("justice", 2.0),
        ("immigration", 2.0),
        ("international", 2.0),
        ("sport", 1.0),
        (None, 1.0),
        ("", 1.0),
    ],
)
def test_theme_risk(theme: str | None, risk: float) -> None:
    assert s.theme_risk(theme) == risk


@pytest.mark.parametrize(
    ("state", "factor"),
    [(None, 1.0), ("active", 1.0), ("closed", 1.0), ("watch", 2.0), ("surge", 3.0), ("alert", 5.0)],
)
def test_wave_factor(state: str | None, factor: float) -> None:
    assert s.wave_factor(state) == factor


# ---------------------------------------------------------------- priority


def prio(**kw: object) -> float:
    base: dict[str, object] = {
        "views": 1000,
        "observers": 5,
        "velocity": 1.0,
        "theme": "economy",
        "verifiability": 0.5,
    }
    base.update(kw)
    return s.priority(s.PriorityInput(**base))  # type: ignore[arg-type]


def test_priority_is_the_product_of_its_factors() -> None:
    expected = s.reach(1000, 5, 1.0) * 2 * 0.5 * 1 * 1 / 1
    assert prio() == pytest.approx(expected)


def test_priority_factors() -> None:
    assert prio(theme="health") == pytest.approx(prio(theme="sport") * 3)
    assert prio(revalidation=True) == pytest.approx(prio() * 0.3)
    assert prio(narrative_state="surge") == pytest.approx(prio() * 3)
    assert prio(narrative_state="alert") == pytest.approx(prio() * 5)
    assert prio(level_cost_cents=10) == pytest.approx(prio() / 10)
    assert prio(level_cost_cents=0.1) == pytest.approx(prio())  # cost floored at 1 cent
    assert prio(verifiability=0) == 0
    assert prio(verifiability=3) == pytest.approx(prio(verifiability=1))  # clamped


def test_priority_has_no_author_term() -> None:
    fields = set(s.PriorityInput.__dataclass_fields__)
    assert not fields & {"author", "account", "followers", "side", "political"}


# --------------------------------------------------------------- promotion


def test_no_promotion_below_every_threshold() -> None:
    m = metrics(observers_6h=4, velocity=2.9, views=50_000, reach=100.0)
    assert s.promotion_reasons(m, THRESHOLDS) == []


def test_each_reason_alone() -> None:
    assert s.promotion_reasons(metrics(observers_6h=5), THRESHOLDS) == [s.REASON_OBSERVERS]
    assert s.promotion_reasons(metrics(velocity=3.0), THRESHOLDS) == [s.REASON_VELOCITY]
    assert s.promotion_reasons(metrics(views=50_001), THRESHOLDS) == [s.REASON_VIEWS]
    assert s.promotion_reasons(metrics(narrative_state="surge"), THRESHOLDS) == [s.REASON_NARRATIVE]
    assert s.promotion_reasons(metrics(narrative_state="alert"), THRESHOLDS) == [s.REASON_NARRATIVE]
    assert s.promotion_reasons(metrics(narrative_state="watch"), THRESHOLDS) == []


def test_light_signal_needs_reach_above_the_median() -> None:
    false = LightSignal.LIKELY_FALSE.value
    assert s.promotion_reasons(metrics(light_signal=false, reach=4.1), THRESHOLDS) == [
        s.REASON_LIGHT_SIGNAL
    ]
    assert s.promotion_reasons(metrics(light_signal=false, reach=4.0), THRESHOLDS) == []
    true = LightSignal.LIKELY_TRUE.value
    assert s.promotion_reasons(metrics(light_signal=true, reach=99.0), THRESHOLDS) == []


def test_reasons_accumulate() -> None:
    m = metrics(observers_6h=9, velocity=5.0, views=60_000)
    assert s.promotion_reasons(m, THRESHOLDS) == [
        s.REASON_OBSERVERS,
        s.REASON_VELOCITY,
        s.REASON_VIEWS,
    ]


def test_confirmation_by_fresh_metrics() -> None:
    assert not s.promotion_confirmed([], 10**9, THRESHOLDS)
    assert s.promotion_confirmed([s.REASON_VIEWS], 50_001, THRESHOLDS)
    assert not s.promotion_confirmed([s.REASON_VIEWS], 50_000, THRESHOLDS)
    # A reason that is not metric-based stands whatever the platform says.
    assert s.promotion_confirmed([s.REASON_VIEWS, s.REASON_OBSERVERS], 0, THRESHOLDS)
    assert s.promotion_confirmed([s.REASON_NARRATIVE], 0, THRESHOLDS)


# --------------------------------------------------------------- demotion


def test_demotion_due_after_the_delay() -> None:
    since = NOW - timedelta(hours=24)
    assert s.demotion_due(since, now=NOW, hours=24, investigating=False)
    assert not s.demotion_due(since + timedelta(seconds=1), now=NOW, hours=24, investigating=False)


def test_demotion_never_during_an_investigation_or_without_clock() -> None:
    since = NOW - timedelta(hours=48)
    assert not s.demotion_due(since, now=NOW, hours=24, investigating=True)
    assert not s.demotion_due(None, now=NOW, hours=24, investigating=False)


# -------------------------------------------------------------- light signal


@pytest.mark.parametrize(
    ("band", "signal"),
    [
        (Band.CONTAINS, LightSignal.LIKELY_TRUE),
        (Band.WITHIN_1_TAU, LightSignal.NEEDS_CONTEXT),
        (Band.WITHIN_3_TAU, LightSignal.LIKELY_FALSE),
        (Band.BEYOND, LightSignal.LIKELY_FALSE),
        ("contains", LightSignal.LIKELY_TRUE),
        ("beyond", LightSignal.LIKELY_FALSE),
    ],
)
def test_light_signal_from_band(band: Band | str, signal: LightSignal) -> None:
    assert s.light_signal_from_band(band) is signal
