"""Shared budget ledger: monthly counters (``budget_counters``), the free daily allocation
(``llm_free_usage``) and the reservations that hold them (``budget_reservations``).

Used by the model router and the radar. Protocol of a paid call:

1. ``reserve_budget`` (own transaction): ``reserved += estimate`` on every capped scope,
   all or nothing, and one ``budget_reservations`` row per held counter.
2. The external call, with no transaction open.
3. ``settle_budget`` (caller's transaction): deletes the reservation rows; ONLY the
   rows actually deleted give back their ``reserved`` amount (a reservation already
   released by ``release_expired`` is not decremented twice); ``spent += actual``;
   ``budget.threshold_reached`` published at the first crossing of 50/80/100 %.

``release_expired`` (core maintenance, singleton) releases holds left by a process
that died between steps 1 and 3. See docs/llm.md, "Budgets".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.db.database import Database
from tower.core.domain.llm import GLOBAL_SCOPE

__all__ = [
    "GLOBAL_SCOPE",
    "BudgetHold",
    "BudgetRefused",
    "FreeHold",
    "release_expired",
    "reserve_budget",
    "reserve_free",
    "settle_budget",
    "settle_free",
]


class BudgetRefused(Exception):
    """A capped scope cannot hold the estimate. ``retry_at``: start of the next period."""

    def __init__(self, scope: str, retry_at: datetime) -> None:
        super().__init__(f"budget exhausted for scope {scope}")
        self.scope = scope
        self.retry_at = retry_at


@dataclass(frozen=True, slots=True)
class BudgetHold:
    reservation_id: int
    counter_id: int
    scope: str
    period_start: date
    amount: int
    """Reserved amount, USD micros."""


@dataclass(frozen=True, slots=True)
class FreeHold:
    reservation_id: int
    provider: str
    free_group: str
    day: date
    tokens: int


async def reserve_budget(
    db: Database,
    scopes: tuple[str, ...],
    amount: int,
    *,
    ref: str,
    now: datetime,
    ttl_s: int,
) -> tuple[BudgetHold, ...]:
    """Reserve ``amount`` (USD micros) on each scope of ``scopes`` (sorted, deduplicated:
    no deadlock between replicas) in ONE transaction of its own.

    A scope with no counter at all is not capped (no hold). The month's counter is
    created from the previous period's cap when missing. Raises ``BudgetRefused`` (and
    rolls everything back) when one capped scope refuses. ``ref``: short, safe
    identifier of the call (``llm:<role>``, ``radar:poll:<account>``), never a secret.
    """
    raise NotImplementedError


async def settle_budget(
    session: AsyncSession, holds: tuple[BudgetHold, ...], actual: int, *, actor: str
) -> None:
    """Charge ``actual`` (USD micros) to each held counter and release the holds (see
    the module docstring). Publishes ``budget.threshold_reached`` in ``session``."""
    raise NotImplementedError


async def reserve_free(
    db: Database,
    *,
    provider: str,
    free_group: str,
    day: date,
    tokens: int,
    need: int,
    allowance: int,
    ref: str,
    now: datetime,
    ttl_s: int,
) -> FreeHold | None:
    """Reserve ``tokens`` of the free daily allocation when ``used + need <= allowance``
    (own transaction). None when refused."""
    raise NotImplementedError


async def settle_free(session: AsyncSession, hold: FreeHold, actual_tokens: int) -> None:
    """Adjust the free usage to the real count and release the hold (no double release)."""
    raise NotImplementedError


async def release_expired(session: AsyncSession, now: datetime, *, limit: int = 500) -> int:
    """Delete expired reservations (``FOR UPDATE SKIP LOCKED``) and give back their amount
    to ``budget_counters.reserved_usd_micros`` / ``llm_free_usage.tokens`` (never below 0).
    Returns the number released."""
    raise NotImplementedError
