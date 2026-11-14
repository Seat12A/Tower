"""Verdict reuse decision (identical signature or validated band)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from tower.core.domain.factcheck.values import Regime, ReuseRule, VerdictStatus

__all__ = ["ReuseContext", "can_reuse", "inheritable_note"]


@dataclass(frozen=True, slots=True)
class ReuseContext:
    level: str  # "N0".."N4"
    direct_reuse_allowed: bool  # profile.level(level).direct_reuse
    verdict_expires_at: datetime | None
    verdict_status: str | None  # status of the candidate verdict
    note_regime: str | None  # regime of the candidate verdict's note (None if no note)
    note_expires_at: datetime | None
    now: datetime


def can_reuse(ctx: ReuseContext, rule: ReuseRule) -> bool:
    """Whether the candidate verdict may cover a claim. N4 never reuses directly."""
    del rule  # same safety checks for both rules
    if ctx.level == "N4" or not ctx.direct_reuse_allowed:
        return False
    if ctx.verdict_status != VerdictStatus.ACTIVE.value:
        return False
    return not (ctx.verdict_expires_at is not None and ctx.verdict_expires_at <= ctx.now)


def inheritable_note(ctx: ReuseContext, rule: ReuseRule) -> bool:
    """Whether the candidate verdict's note may be shown with the reused verdict."""
    if ctx.note_regime is None:
        return False
    if ctx.note_regime == Regime.PROVISIONAL.value:
        return False
    if ctx.note_expires_at is not None and ctx.note_expires_at <= ctx.now:
        return False
    return not (rule is ReuseRule.BAND and ctx.note_regime == Regime.FORMULATION.value)
