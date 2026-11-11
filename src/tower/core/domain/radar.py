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


def base_interval(priority: int, bounds: IntervalBounds) -> int:
    """Base interval (seconds) of a priority, clamped to the bounds."""
    raise NotImplementedError


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
    raise NotImplementedError


def daily_read_allowance(
    *,
    remaining_usd_micros: int | None,
    days_left: int,
    avg_read_cost_usd_micros: int,
) -> int | None:
    """Polls allowed today: remaining budget / days left (today included, >= 1) /
    average cost of one poll. None when the scope is not capped. Never negative."""
    raise NotImplementedError
