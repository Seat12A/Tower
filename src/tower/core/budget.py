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
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.db.database import Database
from tower.core.domain.llm import GLOBAL_SCOPE, crossed_thresholds, month_period
from tower.core.events.outbox import publish
from tower.core.logging import get_logger

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

log = get_logger(__name__)

_ENSURE_COUNTER = text(
    """
    INSERT INTO budget_counters (scope, period_start, period_end, cap_usd_micros)
    SELECT scope, CAST(:ps AS date), CAST(:pe AS date), cap_usd_micros
    FROM budget_counters
    WHERE scope = :scope AND period_start < CAST(:ps AS date)
    ORDER BY period_start DESC LIMIT 1
    ON CONFLICT (scope, period_start) DO NOTHING
    """
)
_RESERVE_BUDGET = text(
    """
    UPDATE budget_counters
    SET reserved_usd_micros = reserved_usd_micros + :est, updated_at = now()
    WHERE scope = :scope AND period_start = CAST(:ps AS date)
      AND spent_usd_micros + reserved_usd_micros + :est <= cap_usd_micros
    RETURNING id
    """
)
_COUNTER_EXISTS = text(
    "SELECT 1 FROM budget_counters WHERE scope = :scope AND period_start = CAST(:ps AS date)"
)
_INSERT_RESERVATION = text(
    """
    INSERT INTO budget_reservations
        (kind, scope, counter_id, period_start, amount, ref, expires_at)
    VALUES (:kind, :scope, :counter_id, CAST(:ps AS date), :amount, :ref,
            CAST(:expires_at AS timestamptz))
    RETURNING id
    """
)
_DELETE_RESERVATION = text("DELETE FROM budget_reservations WHERE id = :rid RETURNING amount")
_SETTLE_BUDGET = text(
    """
    UPDATE budget_counters
    SET spent_usd_micros = spent_usd_micros + :actual,
        reserved_usd_micros = GREATEST(reserved_usd_micros - :released, 0),
        updated_at = now()
    WHERE id = :id
    RETURNING spent_usd_micros, cap_usd_micros
    """
)
_MARK_THRESHOLD = text(
    """
    UPDATE budget_counters
    SET thresholds_notified = array_append(thresholds_notified, CAST(:t AS smallint))
    WHERE id = :id AND NOT (CAST(:t AS smallint) = ANY(thresholds_notified))
    RETURNING id
    """
)
_RESERVE_FREE = text(
    """
    INSERT INTO llm_free_usage (provider, free_group, day, tokens)
    SELECT CAST(:provider AS text), CAST(:grp AS text), CAST(:day AS date), CAST(:est AS bigint)
    WHERE CAST(:need AS bigint) <= CAST(:allowance AS bigint)
    ON CONFLICT (provider, free_group, day) DO UPDATE
    SET tokens = llm_free_usage.tokens + EXCLUDED.tokens
    WHERE llm_free_usage.tokens + CAST(:need AS bigint) <= CAST(:allowance AS bigint)
    RETURNING tokens
    """
)
_SETTLE_FREE = text(
    """
    UPDATE llm_free_usage SET tokens = GREATEST(tokens + :delta, 0)
    WHERE provider = :provider AND free_group = :grp AND day = CAST(:day AS date)
    """
)
_RELEASE_EXPIRED = text(
    """
    DELETE FROM budget_reservations WHERE id IN (
        SELECT id FROM budget_reservations
        WHERE expires_at <= CAST(:now AS timestamptz)
        ORDER BY expires_at
        LIMIT :limit
        FOR UPDATE SKIP LOCKED
    )
    RETURNING kind, counter_id, scope, period_start, amount
    """
)
_GIVE_BACK_BUDGET = text(
    """
    UPDATE budget_counters
    SET reserved_usd_micros = GREATEST(reserved_usd_micros - :amount, 0), updated_at = now()
    WHERE id = :id
    """
)
_GIVE_BACK_FREE = text(
    """
    UPDATE llm_free_usage SET tokens = GREATEST(tokens - :amount, 0)
    WHERE provider = :provider AND free_group = :grp AND day = CAST(:day AS date)
    """
)


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
    if amount <= 0:
        return ()
    start, end = month_period(now)
    expires_at = now + timedelta(seconds=ttl_s)
    holds: list[BudgetHold] = []
    async with db.transaction() as session:
        for scope in sorted(set(scopes)):
            params = {"scope": scope, "ps": start, "pe": end}
            await session.execute(_ENSURE_COUNTER, params)
            counter_id = (
                await session.execute(_RESERVE_BUDGET, {**params, "est": amount})
            ).scalar_one_or_none()
            if counter_id is None:
                if (await session.execute(_COUNTER_EXISTS, params)).first() is None:
                    continue  # no counter at all: not capped
                # Raising rolls back every reservation made so far.
                raise BudgetRefused(scope, datetime.combine(end, datetime.min.time(), UTC))
            reservation_id = (
                await session.execute(
                    _INSERT_RESERVATION,
                    {
                        "kind": "budget",
                        "scope": scope,
                        "counter_id": counter_id,
                        "ps": start,
                        "amount": amount,
                        "ref": ref,
                        "expires_at": expires_at,
                    },
                )
            ).scalar_one()
            holds.append(BudgetHold(int(reservation_id), int(counter_id), scope, start, amount))
    return tuple(holds)


async def settle_budget(
    session: AsyncSession, holds: tuple[BudgetHold, ...], actual: int, *, actor: str
) -> None:
    """Charge ``actual`` (USD micros) to each held counter and release the holds (see
    the module docstring). Publishes ``budget.threshold_reached`` in ``session``."""
    for hold in holds:
        released = (
            await session.execute(_DELETE_RESERVATION, {"rid": hold.reservation_id})
        ).scalar_one_or_none()
        row = (
            await session.execute(
                _SETTLE_BUDGET,
                {"id": hold.counter_id, "released": int(released or 0), "actual": actual},
            )
        ).one()
        spent_after, cap = int(row[0]), int(row[1])
        for threshold in crossed_thresholds(spent_after - actual, spent_after, cap):
            marked = (
                await session.execute(_MARK_THRESHOLD, {"id": hold.counter_id, "t": threshold})
            ).first()
            if marked is not None:
                await publish(
                    session,
                    "budget.threshold_reached",
                    {
                        "scope": hold.scope,
                        "period_start": hold.period_start.isoformat(),
                        "threshold_pct": threshold,
                        "spent_usd_micros": spent_after,
                        "cap_usd_micros": cap,
                    },
                    aggregate_type="budget",
                    aggregate_id=hold.scope,
                    actor=actor,
                )


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
    async with db.transaction() as session:
        reserved = (
            await session.execute(
                _RESERVE_FREE,
                {
                    "provider": provider,
                    "grp": free_group,
                    "day": day,
                    "est": tokens,
                    "need": need,
                    "allowance": allowance,
                },
            )
        ).first()
        if reserved is None:
            return None
        reservation_id = 0
        if tokens > 0:  # the table requires amount > 0
            reservation_id = int(
                (
                    await session.execute(
                        _INSERT_RESERVATION,
                        {
                            "kind": "free",
                            "scope": f"{provider}:{free_group}",
                            "counter_id": None,
                            "ps": day,
                            "amount": tokens,
                            "ref": ref,
                            "expires_at": now + timedelta(seconds=ttl_s),
                        },
                    )
                ).scalar_one()
            )
    return FreeHold(reservation_id, provider, free_group, day, tokens)


async def settle_free(session: AsyncSession, hold: FreeHold, actual_tokens: int) -> None:
    """Adjust the free usage to the real count and release the hold (no double release)."""
    released = (
        await session.execute(_DELETE_RESERVATION, {"rid": hold.reservation_id})
    ).scalar_one_or_none()
    # Released by maintenance already: the reserved tokens were given back, charge it all.
    delta = actual_tokens - hold.tokens if released is not None else actual_tokens
    await session.execute(
        _SETTLE_FREE,
        {"delta": delta, "provider": hold.provider, "grp": hold.free_group, "day": hold.day},
    )


async def release_expired(session: AsyncSession, now: datetime, *, limit: int = 500) -> int:
    """Delete expired reservations (``FOR UPDATE SKIP LOCKED``) and give back their amount
    to ``budget_counters.reserved_usd_micros`` / ``llm_free_usage.tokens`` (never below 0).
    Returns the number released."""
    rows = (await session.execute(_RELEASE_EXPIRED, {"now": now, "limit": limit})).all()
    budget: dict[int, int] = {}
    free: dict[tuple[str, str, date], int] = {}
    for kind, counter_id, scope, period_start, amount in rows:
        if kind == "budget":
            budget[int(counter_id)] = budget.get(int(counter_id), 0) + int(amount)
        else:
            provider, _, group = str(scope).partition(":")
            key = (provider, group, period_start)
            free[key] = free.get(key, 0) + int(amount)
    for counter_id in sorted(budget):  # fixed order: no deadlock between replicas
        await session.execute(_GIVE_BACK_BUDGET, {"id": counter_id, "amount": budget[counter_id]})
    for (provider, group, day), amount in sorted(free.items()):
        await session.execute(
            _GIVE_BACK_FREE, {"provider": provider, "grp": group, "day": day, "amount": amount}
        )
    if rows:
        log.info("budget.reservations_released", count=len(rows))
    return len(rows)
