"""Second-opinion comparison and escalation decision (pipeline.md E8, Q3)."""

from __future__ import annotations

from dataclasses import dataclass

from tower.core.domain.factcheck.values import Agreement, Depth, Veracity, side_of

__all__ = ["EscalationInput", "compare_opinions", "initial_depth", "should_escalate"]


def compare_opinions(judge: Veracity, second: Veracity) -> Agreement:
    """``IDENTICAL`` for the same outcome; ``STYLE`` for one notch apart on the same side
    (true/mostly_true, misleading/false); ``SUBSTANTIVE`` for a side change or a verdict
    against an abstention (``unverifiable``)."""
    if judge == second:
        return Agreement.IDENTICAL
    if side_of(judge) != side_of(second):
        return Agreement.SUBSTANTIVE
    return Agreement.STYLE


@dataclass(frozen=True, slots=True)
class EscalationInput:
    level: str
    """``N3`` or ``N4``."""
    depth: Depth
    agreement: Agreement
    conflicting_sources: bool
    confidence: float
    """Provisional confidence computed after the second opinion."""
    spent_usd_micros: int
    reinforced_cap_usd_micros: int
    """Per-investigation cap of the reinforced depth (no escalation when reached)."""
    threshold: float = 0.6


def should_escalate(value: EscalationInput) -> bool:
    """True when a standard investigation must switch to the reinforced depth: substantive
    disagreement, conflicting sources or confidence below ``threshold``, and the spend so
    far is below the reinforced cap. Already reinforced → False. (N4 investigations start
    reinforced, so they never escalate: see ``initial_depth``.)"""
    if value.depth is Depth.REINFORCED:
        return False
    if value.spent_usd_micros >= value.reinforced_cap_usd_micros:
        return False
    return (
        value.agreement is Agreement.SUBSTANTIVE
        or value.conflicting_sources
        or value.confidence < value.threshold
    )


def initial_depth(level: str) -> Depth:
    """``REINFORCED`` for N4, ``STANDARD`` otherwise."""
    return Depth.REINFORCED if level == "N4" else Depth.STANDARD
