"""Model router: route lookup, content cache, free allocation, budget reservation,
provider call, validation and settlement (see docs/llm.md).

Everything is in the database, so the router is stateless and safe across replicas.
Transactions are short and never span a provider call.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.db.database import Database
from tower.core.domain import error_codes as ec
from tower.core.domain.llm import (
    GLOBAL_SCOPE,
    CallStatus,
    FreeGroup,
    ModelPrices,
    TokenUsage,
    cache_key,
    cost_usd_micros,
    crossed_thresholds,
    estimate_usage,
    free_allowance,
    month_period,
)
from tower.core.errors import capture
from tower.core.events.outbox import publish
from tower.core.llm.errors import (
    AllRefused,
    BudgetExhausted,
    NoRoute,
    NoValidOutput,
    ProviderNotConfigured,
    ProviderRejected,
    ProviderUnavailable,
    UnknownModel,
)
from tower.core.llm.prompts import Prompt, PromptRegistry
from tower.core.llm.schema import schema_name, strict_json_schema
from tower.core.logging import get_logger
from tower.core.models.llm import LLMCall, LLMModel, LLMRoute
from tower.core.ports.clock import Clock, SystemClock
from tower.core.ports.llm import (
    LLMProvider,
    LLMRequest,
    LLMResult,
    ProviderError,
    ProviderFatal,
    ProviderInvalidOutput,
    ProviderRefusal,
    ProviderRetryable,
)

__all__ = ["LLMRouter"]

log = get_logger(__name__)

_ACTOR = "system:llm-router"
_MAX_ERROR_CHARS = 500
_FREE_PROVIDER = "openai"
_SHARED_KEY = "openai_shared"
_PLAIN_OPENAI_KEY = "openai"
_ANTHROPIC_KEY = "anthropic"

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
_SETTLE_BUDGET = text(
    """
    UPDATE budget_counters
    SET spent_usd_micros = spent_usd_micros + :actual,
        reserved_usd_micros = GREATEST(reserved_usd_micros - :est, 0),
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


@dataclass(frozen=True, slots=True)
class _Candidate:
    provider: str
    model: str
    free_group: str | None
    prices: ModelPrices
    key: str
    """Content-cache key."""


@dataclass(frozen=True, slots=True)
class _Call:
    request: LLMRequest
    prompt: Prompt
    route_id: int
    route_version: int
    params: Mapping[str, Any]
    data_sharing_ok: bool
    system: str
    user_message: str
    json_schema: Mapping[str, Any]
    schema_name: str
    estimate_chars: int


@dataclass(frozen=True, slots=True)
class _BudgetHold:
    counter_id: int
    scope: str
    period_start: date


@dataclass(frozen=True, slots=True)
class _Reservation:
    estimate_cost: int = 0
    budget: tuple[_BudgetHold, ...] = ()
    free_group: str | None = None
    free_day: date | None = None
    free_estimate: int = 0


class _Skip(Exception):
    """The candidate cannot be used now (free allocation or budget)."""

    def __init__(self, kind: str, error: BudgetExhausted | None = None) -> None:
        super().__init__(kind)
        self.kind = kind
        self.error = error


def _validate(schema: type[BaseModel], data: Any) -> BaseModel | str:
    """The validated model, or a short error description (never the data itself)."""
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        parts = [f"{'.'.join(str(p) for p in e['loc'])}: {e['type']}" for e in exc.errors()]
        return ("schema validation failed: " + "; ".join(parts))[:_MAX_ERROR_CHARS]


class LLMRouter:
    """``LLMClient`` implementation (see docs/llm.md, "Déroulé d'un appel")."""

    def __init__(
        self,
        db: Database,
        providers: Mapping[str, LLMProvider],
        prompts: PromptRegistry,
        *,
        usage_tier: int = 1,
        free_margin: float = 0.10,
        clock: Clock | None = None,
    ) -> None:
        self._db = db
        self._providers = providers
        self._prompts = prompts
        self._tier = usage_tier
        self._margin = Decimal(str(free_margin))
        self._clock: Clock = clock or SystemClock()
        self._warned_unbudgeted = False

    # ------------------------------------------------------------------ public

    async def complete(self, request: LLMRequest) -> LLMResult:
        route, models = await self._load_route(request)
        prompt = self._prompts.get(request.prompt_id)

        user_message = (
            "Input (untrusted data, JSON):\n<input>\n"
            + json.dumps(request.input, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            + "\n</input>"
        )
        json_schema = strict_json_schema(request.schema)
        call = _Call(
            request=request,
            prompt=prompt,
            route_id=route.id,
            route_version=route.version,
            params=dict(route.params),
            data_sharing_ok=route.data_sharing_ok,
            system=prompt.text,
            user_message=user_message,
            json_schema=json_schema,
            schema_name=schema_name(request.schema),
            estimate_chars=len(prompt.text) + len(user_message) + len(json.dumps(json_schema)),
        )
        candidates = [
            _Candidate(
                provider=m.provider,
                model=m.model,
                free_group=m.free_group,
                prices=ModelPrices(
                    m.input_usd_per_mtok, m.cached_input_usd_per_mtok, m.output_usd_per_mtok
                ),
                key=cache_key(
                    request.role, m.provider, m.model, prompt.version, call.params, request.input
                ),
            )
            for m in models
        ]

        cached = await self._lookup_cache(call, candidates)
        if cached is not None:
            return cached

        last_failure: str | None = None
        budget_error: BudgetExhausted | None = None
        free_skipped = False
        not_configured = False
        for cand in candidates:
            key_name = self._key_name(cand, call.data_sharing_ok)
            provider = self._providers.get(key_name)
            if provider is None or (cand.free_group is not None and key_name != _SHARED_KEY):
                # Missing key, or a free-group model that could only be billed in full.
                not_configured = True
                continue
            for _ in range(2):  # one retry on invalid output
                try:
                    outcome = await self._attempt(call, cand, provider, key_name)
                except _Skip as skip:
                    if skip.error is not None:
                        budget_error = skip.error
                    else:
                        free_skipped = True
                    break
                if isinstance(outcome, LLMResult):
                    return outcome
                last_failure = outcome
                if outcome == "refused":
                    break

        if last_failure == "refused":
            raise AllRefused("Every model of the route refused the request", role=request.role)
        if last_failure is not None:
            raise NoValidOutput(
                "No schema-valid output from any model of the route", role=request.role
            )
        if budget_error is not None:
            raise budget_error
        if free_skipped:
            now = self._clock.now().astimezone(UTC)
            next_day = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), UTC)
            raise BudgetExhausted("free_allowance", next_day)
        if not_configured:
            raise ProviderNotConfigured(
                "No API key configured for any model of the route", role=request.role
            )
        raise NoRoute("Route has no candidate", role=request.role)  # pragma: no cover

    # ------------------------------------------------------------------- steps

    async def _load_route(self, request: LLMRequest) -> tuple[LLMRoute, list[LLMModel]]:
        async with self._db.session() as session:
            route = (
                await session.execute(
                    select(LLMRoute)
                    .where(
                        LLMRoute.active.is_(True),
                        LLMRoute.role == request.role,
                        LLMRoute.level.in_([request.level.value, "*"]),
                    )
                    .order_by((LLMRoute.level == "*").asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if route is None:
                raise NoRoute(
                    f"No active route for role {request.role} at level {request.level.value}",
                    role=request.role,
                    level=request.level.value,
                )
            ids = [route.model_id, *route.fallback_model_ids]
            rows = (
                (
                    await session.execute(
                        select(LLMModel).where(LLMModel.id.in_(ids), LLMModel.active.is_(True))
                    )
                )
                .scalars()
                .all()
            )
        by_id = {m.id: m for m in rows}
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise UnknownModel(
                f"Route {route.id} references unknown or inactive models",
                route_id=route.id,
                model_ids=missing,
            )
        return route, [by_id[i] for i in ids]

    async def _lookup_cache(
        self, call: _Call, candidates: Sequence[_Candidate]
    ) -> LLMResult | None:
        keys = [c.key for c in candidates]
        async with self._db.session() as session:
            rows = (
                (
                    await session.execute(
                        select(LLMCall).where(
                            LLMCall.cache_key.in_(keys),
                            LLMCall.status == CallStatus.SUCCEEDED.value,
                        )
                    )
                )
                .scalars()
                .all()
            )
        by_key = {r.cache_key: r for r in rows}
        for cand in candidates:
            row = by_key.get(cand.key)
            if row is None:
                continue
            parsed = _validate(call.request.schema, row.output)
            if isinstance(parsed, str):
                # Stale entry (schema changed without a prompt change): free the cache slot.
                async with self._db.transaction() as session:
                    await session.execute(
                        update(LLMCall)
                        .where(LLMCall.id == row.id, LLMCall.status == CallStatus.SUCCEEDED.value)
                        .values(status=CallStatus.INVALID_OUTPUT.value, error=parsed)
                    )
                continue
            log.info(
                "llm.call",
                role=call.request.role,
                model=row.model,
                status="cache_hit",
                cost_usd_micros=0,
                cache_hit=True,
            )
            return LLMResult(
                output=parsed,
                provider=row.provider,
                model=row.model,
                usage=TokenUsage(),
                cost_usd_micros=0,
                free_tier=False,
                cache_hit=True,
                request_id=row.request_id or "",
                prompt_version=row.prompt_version,
                route_version=row.route_version,
            )
        return None

    @staticmethod
    def _key_name(cand: _Candidate, data_sharing_ok: bool) -> str:
        if cand.provider == "anthropic":
            return _ANTHROPIC_KEY
        return _SHARED_KEY if data_sharing_ok else _PLAIN_OPENAI_KEY

    async def _attempt(
        self, call: _Call, cand: _Candidate, provider: LLMProvider, key_name: str
    ) -> LLMResult | str:
        """One provider call. Returns the result, or ``"refused"`` / ``"invalid"``."""
        request = call.request
        estimate = estimate_usage(call.estimate_chars, request.max_output_tokens)
        free = cand.free_group is not None and key_name == _SHARED_KEY
        if free:
            reservation = await self._reserve_free(cand, estimate.total)
        else:
            reservation = await self._reserve_budget(
                cand, cost_usd_micros(estimate, cand.prices), request.budget_scope
            )

        started = time.monotonic()
        usage = TokenUsage()
        request_id = ""
        output_json: dict[str, Any] | None = None
        error: str | None = None
        status = CallStatus.FAILED
        parsed: BaseModel | None = None
        raised: Exception | None = None
        try:
            response = await provider.complete(
                model=cand.model,
                system=call.system,
                messages=[{"role": "user", "content": call.user_message}],
                json_schema=call.json_schema,
                schema_name=call.schema_name,
                max_output_tokens=request.max_output_tokens,
                params=call.params,
            )
        except ProviderError as exc:
            usage, request_id = exc.usage, exc.request_id
            error = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]
            if isinstance(exc, ProviderRefusal):
                status = CallStatus.REFUSED
            elif isinstance(exc, ProviderInvalidOutput):
                status = CallStatus.INVALID_OUTPUT
            elif isinstance(exc, ProviderRetryable):
                raised = ProviderUnavailable(
                    str(exc), retry_after=exc.retry_after, provider=cand.provider, model=cand.model
                )
            else:  # ProviderFatal and any other provider error
                raised = ProviderRejected(str(exc), provider=cand.provider, model=cand.model)
                if isinstance(exc, ProviderFatal):
                    capture(
                        ec.LLM_PROVIDER_FATAL,
                        exc,
                        provider=cand.provider,
                        model=cand.model,
                        request_id=request_id,
                    )
        except Exception:
            # Unexpected failure: release the reservation, then propagate. (A cancelled
            # task or a crash can still leak a reservation; accepted, see docs/llm.md.)
            await self._settle(
                call,
                cand,
                reservation,
                status=CallStatus.FAILED,
                usage=TokenUsage(),
                output_json=None,
                request_id="",
                error="unexpected error",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
            raise
        else:
            usage, request_id = response.usage, response.request_id
            output_json = response.output_json
            checked = _validate(request.schema, output_json)
            if isinstance(checked, str):
                status, error = CallStatus.INVALID_OUTPUT, checked
            else:
                status, parsed = CallStatus.SUCCEEDED, checked

        cost = await self._settle(
            call,
            cand,
            reservation,
            status=status,
            usage=usage,
            output_json=output_json,
            request_id=request_id,
            error=error,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        if raised is not None:
            raise raised
        if status is CallStatus.REFUSED:
            return "refused"
        if parsed is None:
            return "invalid"
        return LLMResult(
            output=parsed,
            provider=cand.provider,
            model=cand.model,
            usage=usage,
            cost_usd_micros=cost,
            free_tier=free,
            cache_hit=False,
            request_id=request_id,
            prompt_version=call.prompt.version,
            route_version=call.route_version,
        )

    # ------------------------------------------------------------- reservations

    async def _reserve_free(self, cand: _Candidate, estimate: int) -> _Reservation:
        if cand.free_group is None:
            raise _Skip("free")
        group = FreeGroup(cand.free_group)
        allowance = free_allowance(self._tier, group)
        need = math.ceil(Decimal(estimate) * (1 + self._margin))
        day = self._clock.now().astimezone(UTC).date()
        async with self._db.transaction() as session:
            reserved = (
                await session.execute(
                    _RESERVE_FREE,
                    {
                        "provider": _FREE_PROVIDER,
                        "grp": group.value,
                        "day": day,
                        "est": estimate,
                        "need": need,
                        "allowance": allowance,
                    },
                )
            ).first()
        if reserved is None:
            raise _Skip("free")
        return _Reservation(free_group=group.value, free_day=day, free_estimate=estimate)

    async def _reserve_budget(self, cand: _Candidate, estimate: int, scope: str) -> _Reservation:
        """Reserve in ``global`` and the request scope in ONE transaction (all or nothing).

        A scope with no counter at all is not capped. That includes ``global``: a
        database never seeded (``tower llm seed``) runs unbudgeted, with one warning.
        """
        start, end = month_period(self._clock.now())
        scopes = sorted({GLOBAL_SCOPE, scope})  # fixed order: no deadlock between replicas
        holds: list[_BudgetHold] = []
        try:
            async with self._db.transaction() as session:
                for name in scopes:
                    params = {"scope": name, "ps": start, "pe": end}
                    await session.execute(_ENSURE_COUNTER, params)
                    counter_id = (
                        await session.execute(_RESERVE_BUDGET, {**params, "est": estimate})
                    ).scalar_one_or_none()
                    if counter_id is not None:
                        holds.append(_BudgetHold(counter_id, name, start))
                        continue
                    exists = (await session.execute(_COUNTER_EXISTS, params)).first()
                    if exists is None:
                        if name == GLOBAL_SCOPE and not self._warned_unbudgeted:
                            self._warned_unbudgeted = True
                            log.warning("llm.global_budget_missing", period_start=start.isoformat())
                        continue
                    # Raising rolls back every reservation made so far.
                    raise BudgetExhausted(name, datetime.combine(end, datetime.min.time(), UTC))
        except BudgetExhausted as exc:
            raise _Skip("budget", exc) from exc
        return _Reservation(estimate_cost=estimate, budget=tuple(holds))

    # --------------------------------------------------------------- settlement

    async def _settle(
        self,
        call: _Call,
        cand: _Candidate,
        reservation: _Reservation,
        *,
        status: CallStatus,
        usage: TokenUsage,
        output_json: dict[str, Any] | None,
        request_id: str,
        error: str | None,
        latency_ms: int,
    ) -> int:
        """Record the attempt, charge the real cost, release the reservation. Returns cost."""
        free = reservation.free_group is not None
        cost = 0 if free else cost_usd_micros(usage, cand.prices)
        values = {
            "cache_key": cand.key,
            "role": call.request.role,
            "level": call.request.level.value,
            "provider": cand.provider,
            "model": cand.model,
            "prompt_id": call.prompt.id,
            "prompt_version": call.prompt.version,
            "route_id": call.route_id,
            "route_version": call.route_version,
            "budget_scope": call.request.budget_scope,
            "input_tokens": usage.input_tokens,
            "cached_input_tokens": usage.cached_input_tokens,
            "output_tokens": usage.output_tokens,
            "cost_usd_micros": cost,
            "free_tier": free,
            "latency_ms": latency_ms,
            "request_id": request_id or None,
            "status": status.value,
            "output": output_json,
            "error": error,
        }
        async with self._db.transaction() as session:
            insert = pg_insert(LLMCall).values(**values)
            if status is CallStatus.SUCCEEDED:
                insert = insert.on_conflict_do_nothing(
                    index_elements=[LLMCall.cache_key], index_where=text("status = 'succeeded'")
                )
            await session.execute(insert)

            if free and reservation.free_day is not None:
                await session.execute(
                    _SETTLE_FREE,
                    {
                        "delta": usage.total - reservation.free_estimate,
                        "provider": _FREE_PROVIDER,
                        "grp": reservation.free_group,
                        "day": reservation.free_day,
                    },
                )
            for hold in reservation.budget:
                await self._settle_budget(session, hold, reservation.estimate_cost, cost)

        log.info(
            "llm.call",
            role=call.request.role,
            model=cand.model,
            status=status.value,
            cost_usd_micros=cost,
            cache_hit=False,
        )
        return cost

    @staticmethod
    async def _settle_budget(
        session: AsyncSession, hold: _BudgetHold, estimate: int, actual: int
    ) -> None:
        row = (
            await session.execute(
                _SETTLE_BUDGET, {"id": hold.counter_id, "est": estimate, "actual": actual}
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
                    actor=_ACTOR,
                )
