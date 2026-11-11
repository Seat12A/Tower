from __future__ import annotations

import pytest

from tower.core.domain.radar import (
    IntervalBounds,
    base_interval,
    daily_read_allowance,
    next_interval,
)

B = IntervalBounds()


@pytest.mark.parametrize(
    ("priority", "expected"),
    [(100, 120), (50, 120), (49, 600), (0, 600), (-1, 1800), (-100, 1800)],
)
def test_base_interval_bands(priority: int, expected: int) -> None:
    assert base_interval(priority, B) == expected


def test_base_interval_clamped() -> None:
    bounds = IntervalBounds(high_s=10, normal_s=20, low_s=100_000, min_s=60, max_s=3600)
    assert base_interval(80, bounds) == 60
    assert base_interval(-5, bounds) == 3600


def _next(items: int, empty: int, priority: int = 0, bounds: IntervalBounds = B) -> int:
    return next_interval(priority=priority, new_items=items, consecutive_empty=empty, bounds=bounds)


def test_backoff_doubles_then_caps() -> None:
    assert [_next(0, n) for n in (1, 2, 3, 4, 5, 6)] == [1200, 2400, 4800, 9600, 19200, 21600]
    assert _next(0, 10_000) == B.max_s


def test_activity_divides_base() -> None:
    assert _next(1, 0) == 600
    assert _next(2, 0) == 300
    assert _next(4, 0) == 200
    assert _next(20, 0) == B.min_s  # 600 // 11 = 54, floored


def test_clamped_to_floor() -> None:
    assert _next(100, 0, priority=80) == B.min_s


def test_allowance() -> None:
    assert (
        daily_read_allowance(remaining_usd_micros=None, days_left=5, avg_read_cost_usd_micros=1)
        is None
    )
    assert (
        daily_read_allowance(remaining_usd_micros=0, days_left=5, avg_read_cost_usd_micros=10) == 0
    )
    assert (
        daily_read_allowance(remaining_usd_micros=-5, days_left=5, avg_read_cost_usd_micros=10) == 0
    )
    assert (
        daily_read_allowance(remaining_usd_micros=1000, days_left=10, avg_read_cost_usd_micros=10)
        == 10
    )
    # days_left is at least 1
    assert (
        daily_read_allowance(remaining_usd_micros=1000, days_left=0, avg_read_cost_usd_micros=10)
        == 100
    )
