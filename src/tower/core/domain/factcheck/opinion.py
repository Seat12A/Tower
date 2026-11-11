"""Second-opinion comparison and escalation decision (pipeline.md E8, Q3)."""

from __future__ import annotations

from dataclasses import dataclass

from tower.core.domain.factcheck.values import Agreement, Depth, Veracity

__all__ = ["EscalationInput", "compare_opinions", "initial_depth", "should_escalate"]


def compare_opinions(judge: Veracity, second: Veracity) -> Agreement:
    """``IDENTICAL`` for the same outcome; ``STYLE`` for one notch apart on the same side
    (true/mostly_true, misleading/false); ``SUBSTANTIVE`` for a side change or a verdict
    against an abstention (``unverifiable``)."""
    raise NotImplementedError


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
    raise NotImplementedError


def initial_depth(level: str) -> Depth:
    """``REINFORCED`` for N4, ``STANDARD`` otherwise."""
    raise NotImplementedError
