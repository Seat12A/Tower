"""Value bands: position of a claimed interval relative to a reference value (Q9)."""

from __future__ import annotations

from decimal import Decimal

from tower.core.domain.factcheck.values import Band, BandSide

__all__ = ["band_label", "band_of", "interval_contains", "is_outdated", "tolerance"]


def tolerance(
    reference: Decimal,
    *,
    unit: str | None,
    ratio: Decimal,
    floor_rate_points: Decimal,
    floor_default: Decimal,
) -> Decimal:
    """tau = max(floor, ratio * |reference|); the floor is in points for a percent."""
    floor = floor_rate_points if unit == "percent" else floor_default
    return max(floor, ratio * abs(reference))


def interval_contains(low: Decimal | None, high: Decimal | None, value: Decimal) -> bool:
    """``[low, high[`` (degenerate ``low == high`` is the point itself; None = open)."""
    if low is None and high is None:
        raise ValueError("an interval needs at least one bound")
    if low is not None and high is not None and low == high:
        return value == low
    if low is not None and value < low:
        return False
    return not (high is not None and value >= high)


def band_of(
    low: Decimal | None, high: Decimal | None, reference: Decimal, tau: Decimal
) -> tuple[Band, BandSide]:
    """Classify the interval against the reference value."""
    if interval_contains(low, high, reference):
        return Band.CONTAINS, BandSide.NONE
    if low is not None and reference < low:
        side, distance = BandSide.ABOVE, low - reference
    else:
        if high is None:  # unreachable: not contained and not below `low`
            raise ValueError("inconsistent interval")
        side, distance = BandSide.BELOW, reference - high
    if distance <= tau:
        return Band.WITHIN_1_TAU, side
    if distance <= 3 * tau:
        return Band.WITHIN_3_TAU, side
    return Band.BEYOND, side


def is_outdated(
    low: Decimal | None,
    high: Decimal | None,
    *,
    current: Decimal,
    previous: Decimal | None,
    tau: Decimal,
) -> bool:
    """True iff the interval does not contain ``current`` but contains ``previous``."""
    del tau  # kept for a stable signature: the test is containment, not distance
    if previous is None:
        return False
    return not interval_contains(low, high, current) and interval_contains(low, high, previous)


def band_label(band: Band, side: BandSide) -> str:
    """Display key, e.g. ``above_within_1_tau`` or ``contains``."""
    if band is Band.CONTAINS or side is BandSide.NONE:
        return band.value
    return f"{side.value}_{band.value}"
