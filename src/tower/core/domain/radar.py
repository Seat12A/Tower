"""Adaptive polling of watched accounts (pure: stdlib only). See docs/radar.md.

The interval of an account starts from a base chosen by priority, is divided when the
account publishes often, doubled after each empty read, and always clamped to
``[min_s, max_s]``. The daily read allowance caps how many polls the scheduler enqueues.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["IntervalBounds", "base_interval", "daily_read_allowance", "next_interval"]


@dataclass(frozen=True, slots=True)
class IntervalBounds:
    high_s: int = 120
    """Base for priority >= 50."""
    normal_s: int = 600
    """Base for 0 <= priority < 50."""
    low_s: int = 1800
    """Base for priority < 0."""
    min_s: int = 60
    max_s: int = 21_600


def _clamp(value: int, bounds: IntervalBounds) -> int:
    return max(bounds.min_s, min(bounds.max_s, value))


def base_interval(priority: int, bounds: IntervalBounds) -> int:
    """Base interval (seconds) of a priority, clamped to the bounds."""
    if priority >= 50:
        base = bounds.high_s
    elif priority >= 0:
        base = bounds.normal_s
    else:
        base = bounds.low_s
    return _clamp(base, bounds)


def next_interval(
    *,
    priority: int,
    new_items: int,
    consecutive_empty: int,
    bounds: IntervalBounds,
) -> int:
    """Interval until the next read, after a read that returned ``new_items`` items.

    ``consecutive_empty`` is the count AFTER this read (0 when ``new_items > 0``).
    - ``new_items == 0``: ``base * 2 ** consecutive_empty`` (capped).
    - ``new_items >= 1``: ``base // (1 + new_items // 2)`` — e.g. 1 item keeps the base,
      4 items third it; a page full of new items reaches the floor quickly.
    Always clamped to ``[bounds.min_s, bounds.max_s]``.
    """
    base = base_interval(priority, bounds)
    if new_items <= 0:
        # Cap the exponent: the result is clamped anyway, and this keeps ints small.
        value = base * 2 ** min(max(consecutive_empty, 0), 30)
    else:
        value = base // (1 + new_items // 2)
    return _clamp(value, bounds)


def daily_read_allowance(
    *,
    remaining_usd_micros: int | None,
    days_left: int,
    avg_read_cost_usd_micros: int,
) -> int | None:
    """Polls allowed today: remaining budget / days left (today included, >= 1) /
    average cost of one poll. None when the scope is not capped. Never negative."""
    if remaining_usd_micros is None:
        return None
    per_day = max(remaining_usd_micros, 0) // max(days_left, 1)
    return per_day // max(avg_read_cost_usd_micros, 1)
