"""Embedding service (E1, E3): route and price lookup, bounded in-process cache, budget
reservation, batched provider calls, ``llm_calls`` journal, L2 normalization.

Stateless apart from the bounded cache (a pure optimisation: identical texts are neither
sent nor charged twice). Transactions are short and never span a provider call.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from tower.core.budget import BudgetHold, BudgetRefused, reserve_budget, settle_budget
from tower.core.domain import error_codes as ec
from tower.core.domain.llm import (
    GLOBAL_SCOPE,
    CallStatus,
    ModelPrices,
    TokenUsage,
    cost_usd_micros,
)
from tower.core.errors import capture
from tower.core.factcheck import names
from tower.core.llm.errors import BudgetExhausted, NoRoute, UnknownModel
from tower.core.logging import get_logger
from tower.core.models.llm import LLMCall, LLMModel, LLMRoute
from tower.core.ports.clock import Clock, SystemClock
from tower.core.ports.llm import ProviderError, ProviderFatal, ProviderRetryable

if TYPE_CHECKING:
    from tower.core.db.database import Database
    from tower.core.ports.embeddings import Embedder

__all__ = ["EmbedResult", "EmbeddingService"]

log = get_logger(__name__)

_ACTOR = "system:embeddings"
_PROVIDER = "openai"
_PROMPT_ID = "embed"
_PROMPT_VERSION = "1"
_BATCH = 256
_MAX_ERROR_CHARS = 500


@dataclass(frozen=True, slots=True)
class EmbedResult:
    vectors: list[list[float]]
    """Same order as the input, ``dimensions`` floats each, L2-normalized."""
    model: str
    cost_usd_micros: int
    cache_hits: int


def estimate_tokens(texts: Sequence[str]) -> int:
    """Conservative input estimate: ``ceil(len / 3) + 1`` per text (the port returns
    vectors only, so this is also the billed amount)."""
    return sum(math.ceil(len(t) / 3) + 1 for t in texts)


def _normalize(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(math.fsum(v * v for v in vector))
    if norm == 0.0:
        return [float(v) for v in vector]
    return [float(v) / norm for v in vector]


class EmbeddingService:
    def __init__(
        self,
        db: Database,
        embedder: Embedder,
        *,
        model_name: str | None = None,
        cache_size: int = 4096,
        clock: Clock | None = None,
        reservation_ttl_s: int = 1800,
    ) -> None:
        self._db = db
        self._embedder = embedder
        self.model_name = model_name or embedder.model
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._cache_size = max(0, cache_size)
        self._clock: Clock = clock or SystemClock()
        self._ttl_s = reservation_ttl_s

    # ------------------------------------------------------------------ public

    async def embed(self, texts: Sequence[str], *, level: str, ref: str) -> EmbedResult:
        """Vectors of ``texts`` in order. ``level`` selects the route and the budget scope
        (``level:<N>`` and ``global``); ``ref`` is a short safe label for logs and errors.

        Raises ``BudgetExhausted`` (callers: ``RetryLater``) or ``ProviderRetryable``
        (after ``capture(FCK_EMBEDDING_FAILED)``).
        """
        if not texts:
            return EmbedResult([], self.model_name, 0, 0)
        route_id, route_version, dims, prices = await self._load_route(level)

        keys = [self._cache_key(t) for t in texts]
        found: dict[str, list[float]] = {}
        missing: dict[str, str] = {}  # cache key -> text, unique, input order
        hits = 0
        for key, value in zip(keys, texts, strict=True):
            cached = self._cache_get(key)
            if cached is not None and len(cached) == dims:
                found[key] = cached
                hits += 1
            elif key not in missing:
                missing[key] = value

        total_cost = 0
        items = list(missing.items())
        for start in range(0, len(items), _BATCH):
            chunk = items[start : start + _BATCH]
            vectors, cost = await self._call_batch(
                [t for _, t in chunk],
                level=level,
                ref=ref,
                dims=dims,
                prices=prices,
                route_id=route_id,
                route_version=route_version,
            )
            total_cost += cost
            for (key, _), vector in zip(chunk, vectors, strict=True):
                found[key] = vector
                self._cache_put(key, vector)
        return EmbedResult(
            vectors=[list(found[k]) for k in keys],
            model=self.model_name,
            cost_usd_micros=total_cost,
            cache_hits=hits,
        )

    # ------------------------------------------------------------------- cache

    def _cache_key(self, value: str) -> str:
        return hashlib.sha256((self.model_name + "\0" + value).encode("utf-8")).hexdigest()

    def _cache_get(self, key: str) -> list[float] | None:
        vector = self._cache.get(key)
        if vector is not None:
            self._cache.move_to_end(key)
        return vector

    def _cache_put(self, key: str, vector: list[float]) -> None:
        if self._cache_size == 0:
            return
        self._cache[key] = vector
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    # ------------------------------------------------------------------- route

    async def _load_route(self, level: str) -> tuple[int, int, int, ModelPrices]:
        async with self._db.session() as session:
            route = (
                await session.execute(
                    select(LLMRoute)
                    .where(
                        LLMRoute.active.is_(True),
                        LLMRoute.role == names.ROLE_EMBED,
                        LLMRoute.level.in_([level, "*"]),
                    )
                    .order_by((LLMRoute.level == "*").asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if route is None:
                raise NoRoute(
                    f"No active route for role {names.ROLE_EMBED} at level {level}",
                    role=names.ROLE_EMBED,
                    level=level,
                )
            model = await session.get(LLMModel, route.model_id)
        if model is None or not model.active:
            raise UnknownModel(
                f"Route {route.id} references an unknown or inactive model", route_id=route.id
            )
        dims = int(route.params.get("dimensions", 512))
        prices = ModelPrices(
            model.input_usd_per_mtok, model.cached_input_usd_per_mtok, model.output_usd_per_mtok
        )
        return route.id, route.version, dims, prices

    # ---------------------------------------------------------------- provider

    async def _call_batch(
        self,
        batch: Sequence[str],
        *,
        level: str,
        ref: str,
        dims: int,
        prices: ModelPrices,
        route_id: int,
        route_version: int,
    ) -> tuple[list[list[float]], int]:
        usage = TokenUsage(input_tokens=estimate_tokens(batch))
        cost = cost_usd_micros(usage, prices)
        scope = names.level_scope(level)
        now: datetime = self._clock.now()
        try:
            holds = await reserve_budget(
                self._db,
                (GLOBAL_SCOPE, scope),
                cost,
                ref=f"llm:{names.ROLE_EMBED}",
                now=now,
                ttl_s=self._ttl_s,
            )
        except BudgetRefused as exc:
            raise BudgetExhausted(exc.scope, exc.retry_at) from exc

        started = time.monotonic()
        error: str | None = None
        raised: ProviderError | None = None
        vectors: list[list[float]] = []
        try:
            raw = await self._embedder.embed(list(batch), dimensions=dims)
        except ProviderError as exc:
            error = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]
            capture(ec.FCK_EMBEDDING_FAILED, exc, ref=ref, level=level)
            retry_after = exc.retry_after if isinstance(exc, ProviderRetryable) else None
            raised = ProviderRetryable(str(exc), retry_after=retry_after, request_id=exc.request_id)
        except Exception:
            await self._settle(
                holds, 0, batch, level, scope, route_id, route_version, started, False, None
            )
            raise
        else:
            problem = self._check(raw, len(batch), dims)
            if problem is not None:
                error = problem
                capture(ec.FCK_EMBEDDING_FAILED, ValueError(problem), ref=ref, level=level)
                raised = ProviderFatal(problem)
            else:
                vectors = [_normalize(v) for v in raw]

        ok = raised is None
        charged = cost if ok else 0
        await self._settle(
            holds, charged, batch, level, scope, route_id, route_version, started, ok, error
        )
        if raised is not None:
            raise raised
        return vectors, charged

    @staticmethod
    def _check(raw: Sequence[Sequence[float]], expected: int, dims: int) -> str | None:
        if len(raw) != expected:
            return f"provider returned {len(raw)} vectors for {expected} texts"
        for vector in raw:
            if len(vector) != dims:
                return f"provider returned {len(vector)} dimensions, expected {dims}"
        return None

    async def _settle(
        self,
        holds: tuple[BudgetHold, ...],
        cost: int,
        batch: Sequence[str],
        level: str,
        scope: str,
        route_id: int,
        route_version: int,
        started: float,
        ok: bool,
        error: str | None,
    ) -> None:
        digest = hashlib.sha256(
            json.dumps([self.model_name, *batch], ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        tokens = estimate_tokens(batch) if ok else 0
        values = {
            "role": names.ROLE_EMBED,
            "level": level,
            "provider": _PROVIDER,
            "model": self.model_name,
            "prompt_id": _PROMPT_ID,
            "prompt_version": _PROMPT_VERSION,
            "route_id": route_id,
            "route_version": route_version,
            "budget_scope": scope,
            "input_tokens": tokens,
            "cost_usd_micros": cost,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "status": (CallStatus.SUCCEEDED if ok else CallStatus.FAILED).value,
            "error": error,
        }
        async with self._db.transaction() as session:
            inserted = None
            if ok:
                inserted = (
                    await session.execute(
                        pg_insert(LLMCall)
                        .values(cache_key=digest, **values)
                        .on_conflict_do_nothing(
                            index_elements=[LLMCall.cache_key],
                            index_where=text("status = 'succeeded'"),
                        )
                        .returning(LLMCall.id)
                    )
                ).first()
            if not ok or inserted is None:
                # A failed call, or the same batch sent again after the process cache was
                # lost: the journal must still show what was charged.
                suffix = "" if not ok else ":" + uuid.uuid4().hex[:8]
                row = dict(values)
                if inserted is None and ok:
                    row["status"] = CallStatus.SUCCEEDED.value
                await session.execute(
                    pg_insert(LLMCall).values(cache_key=f"{digest}{suffix}", **row)
                )
            if holds:
                await settle_budget(session, holds, cost, actor=_ACTOR)
        log.info(
            "embed.call",
            texts=len(batch),
            level=level,
            status="succeeded" if ok else "failed",
            cost_usd_micros=cost,
        )
