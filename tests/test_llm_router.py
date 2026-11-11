from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import text

from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain.llm import Level, TokenUsage, month_period
from tower.core.jobs.queue import PermanentError, RetryLater
from tower.core.llm.admin import run_seed, run_usage
from tower.core.llm.errors import (
    AllRefused,
    BudgetExhausted,
    LLMRouterError,
    NoRoute,
    NoValidOutput,
    ProviderNotConfigured,
    ProviderRejected,
    ProviderUnavailable,
    UnknownModel,
    UnknownPrompt,
    to_job_error,
)
from tower.core.llm.prompts import PromptRegistry
from tower.core.llm.router import LLMRouter
from tower.core.ports.llm import (
    LLMProvider,
    LLMRequest,
    ProviderFatal,
    ProviderInvalidOutput,
    ProviderRefusal,
    ProviderResponse,
    ProviderRetryable,
)


class Answer(BaseModel):
    verdict: str


def ok(verdict: str = "true", usage: TokenUsage | None = None) -> ProviderResponse:
    return ProviderResponse(
        output_json={"verdict": verdict},
        usage=usage or TokenUsage(input_tokens=10, output_tokens=20),
        request_id="req-1",
        stop_reason="end",
    )


class FakeProvider:
    """Replays a script (last item repeats); exceptions are raised."""

    def __init__(self, *script: ProviderResponse | Exception, name: str = "fake") -> None:
        self.name = name
        self.script = list(script) or [ok()]
        self.calls: list[dict[str, Any]] = []

    async def complete(self, **kwargs: Any) -> ProviderResponse:
        self.calls.append(kwargs)
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def models(self) -> list[str]:
        return [c["model"] for c in self.calls]


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


@pytest.fixture
def prompts(tmp_path: Path) -> PromptRegistry:
    (tmp_path / "check.md").write_text("You are a careful fact checker.\n")
    return PromptRegistry([tmp_path])


async def add_model(
    db: Database,
    provider: str,
    model: str,
    *,
    free_group: str | None = None,
    inp: str = "0",
    out: str = "1",
    active: bool = True,
) -> int:
    async with db.transaction() as session:
        result = await session.execute(
            text(
                "INSERT INTO llm_models (provider, model, input_usd_per_mtok,"
                " cached_input_usd_per_mtok, output_usd_per_mtok, free_group,"
                " prices_checked_on, active) VALUES (:p, :m, :i, :i, :o, :g,"
                " '2026-11-10', :a) RETURNING id"
            ),
            {"p": provider, "m": model, "i": inp, "o": out, "g": free_group, "a": active},
        )
        return int(result.scalar_one())


async def add_route(
    db: Database,
    model_id: int,
    *fallbacks: int,
    role: str = "extract",
    level: str = "*",
    shared: bool = False,
    version: int = 1,
) -> int:
    async with db.transaction() as session:
        result = await session.execute(
            text(
                "INSERT INTO llm_routes (role, level, model_id, fallback_model_ids, params,"
                " data_sharing_ok, version, created_by) VALUES (:r, :l, :m, :f,"
                " CAST('{\"effort\": \"low\"}' AS jsonb), :s, :v, 'test') RETURNING id"
            ),
            {"r": role, "l": level, "m": model_id, "f": list(fallbacks), "s": shared, "v": version},
        )
        return int(result.scalar_one())


async def add_budget(
    db: Database,
    cap: int,
    *,
    scope: str = "global",
    spent: int = 0,
    start: date | None = None,
) -> None:
    ps, pe = month_period(datetime.now(UTC)) if start is None else (start, None)
    if pe is None:
        pe = month_period(datetime(start.year, start.month, 1, tzinfo=UTC))[1]  # type: ignore[union-attr]
    async with db.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO budget_counters (scope, period_start, period_end, cap_usd_micros,"
                " spent_usd_micros) VALUES (:s, :ps, :pe, :c, :sp)"
            ),
            {"s": scope, "ps": ps, "pe": pe, "c": cap, "sp": spent},
        )


async def counter(db: Database, scope: str = "global") -> tuple[int, int, int]:
    async with db.session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT spent_usd_micros, reserved_usd_micros, cap_usd_micros"
                    " FROM budget_counters WHERE scope = :s ORDER BY period_start DESC LIMIT 1"
                ),
                {"s": scope},
            )
        ).one()
    return int(row[0]), int(row[1]), int(row[2])


async def scalar(db: Database, sql: str, **params: Any) -> Any:
    async with db.session() as session:
        return (await session.execute(text(sql), params)).scalar_one()


def make_router(
    db: Database,
    providers: Mapping[str, LLMProvider],
    prompts: PromptRegistry,
    **kwargs: Any,
) -> LLMRouter:
    return LLMRouter(db, providers, prompts, **kwargs)


def request(
    text_: str = "claim", *, level: Level = Level.N1, scope: str = "global", max_out: int = 100
) -> LLMRequest:
    return LLMRequest(
        role="extract",
        level=level,
        prompt_id="check",
        input={"text": text_},
        schema=Answer,
        max_output_tokens=max_out,
        budget_scope=scope,
    )


async def test_second_identical_call_is_served_from_cache(
    db: Database, prompts: PromptRegistry
) -> None:
    mid = await add_model(db, "openai", "paid-a")
    await add_route(db, mid)
    provider = FakeProvider(ok("first"))
    router = make_router(db, {"openai": provider}, prompts)

    first = await router.complete(request())
    second = await router.complete(request())

    assert len(provider.calls) == 1
    assert not first.cache_hit
    assert first.cost_usd_micros == 20
    assert second.cache_hit
    assert second.cost_usd_micros == 0
    assert not second.free_tier
    assert second.usage == TokenUsage()
    assert isinstance(second.output, Answer) and second.output.verdict == "first"
    assert await scalar(db, "SELECT count(*) FROM llm_calls") == 1
    # A different input is a miss.
    await router.complete(request("other"))
    assert len(provider.calls) == 2


async def test_prompt_and_input_framing(db: Database, prompts: PromptRegistry) -> None:
    mid = await add_model(db, "openai", "paid-a")
    await add_route(db, mid)
    provider = FakeProvider()
    await make_router(db, {"openai": provider}, prompts).complete(request("héllo"))
    call = provider.calls[0]
    assert call["system"] == "You are a careful fact checker.\n"
    assert call["messages"] == [
        {
            "role": "user",
            "content": 'Input (untrusted data, JSON):\n<input>\n{"text":"héllo"}\n</input>',
        }
    ]
    assert call["schema_name"] == "Answer"
    assert call["json_schema"]["additionalProperties"] is False
    assert call["params"] == {"effort": "low"}


async def test_versions_are_recorded(db: Database, prompts: PromptRegistry) -> None:
    mid = await add_model(db, "openai", "paid-a")
    route_id = await add_route(db, mid, version=3)
    result = await make_router(db, {"openai": FakeProvider()}, prompts).complete(request())
    version = prompts.get("check").version
    assert result.prompt_version == version
    assert result.route_version == 3
    async with db.session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT prompt_id, prompt_version, route_id, route_version, level,"
                    " budget_scope, status, request_id FROM llm_calls"
                )
            )
        ).one()
    assert tuple(row) == ("check", version, route_id, 3, "N1", "global", "succeeded", "req-1")


async def test_free_allocation_then_paid_fallback_before_exceeding(
    db: Database, prompts: PromptRegistry
) -> None:
    free_id = await add_model(db, "openai", "free-small", free_group="small", out="5")
    paid_id = await add_model(db, "openai", "paid-b", out="1")
    await add_route(db, free_id, paid_id, shared=True)
    await add_budget(db, 10_000_000)
    provider = FakeProvider()
    router = make_router(db, {"openai_shared": provider}, prompts)

    first = await router.complete(request("one"))
    assert first.free_tier and first.cost_usd_micros == 0 and first.model == "free-small"
    tokens = await scalar(db, "SELECT tokens FROM llm_free_usage")
    assert tokens == 30  # reservation adjusted to the real usage
    assert await counter(db) == (0, 0, 10_000_000)

    allowance = 2_500_000
    async with db.transaction() as session:
        await session.execute(text("UPDATE llm_free_usage SET tokens = :t"), {"t": allowance - 10})
    second = await router.complete(request("two"))

    assert provider.models == ["free-small", "paid-b"]
    assert not second.free_tier and second.model == "paid-b" and second.cost_usd_micros == 20
    assert await scalar(db, "SELECT tokens FROM llm_free_usage") == allowance - 10
    assert await counter(db) == (20, 0, 10_000_000)


async def test_free_model_is_never_called_paid(db: Database, prompts: PromptRegistry) -> None:
    free_id = await add_model(db, "openai", "free-small", free_group="small")
    await add_route(db, free_id, shared=True)
    plain = FakeProvider()
    router = make_router(db, {"openai": plain}, prompts)  # shared key missing
    with pytest.raises(ProviderNotConfigured) as info:
        await router.complete(request())
    assert info.value.code.code == "TWR-LLM-008"
    assert plain.calls == []

    # Route without data sharing: the plain key would bill the free-group model in full.
    anthropic_id = await add_model(db, "anthropic", "claude-x")
    await add_route(db, free_id, anthropic_id, role="other", shared=False)
    claude = FakeProvider()
    router = make_router(db, {"openai": plain, "anthropic": claude}, prompts)
    result = await router.complete(LLMRequest("other", Level.N1, "check", {"a": 1}, Answer, 50))
    assert result.model == "claude-x"
    assert plain.calls == [] and claude.models == ["claude-x"]


async def test_free_allowance_exhausted_without_fallback(
    db: Database, prompts: PromptRegistry
) -> None:
    free_id = await add_model(db, "openai", "free-small", free_group="small")
    await add_route(db, free_id, shared=True)
    async with db.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO llm_free_usage (provider, free_group, day, tokens)"
                " VALUES ('openai', 'small', (now() AT TIME ZONE 'UTC')::date, 2500000)"
            )
        )
    provider = FakeProvider()
    with pytest.raises(BudgetExhausted) as info:
        await make_router(db, {"openai_shared": provider}, prompts).complete(request())
    assert info.value.code.code == "TWR-LLM-001"
    assert provider.calls == []


class GatedProvider:
    name = "gated"

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def complete(self, **kwargs: Any) -> ProviderResponse:
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return ok(usage=TokenUsage(input_tokens=5, output_tokens=100))


async def test_concurrent_reservations_never_exceed_cap(
    db: Database, prompts: PromptRegistry
) -> None:
    # 1 micro-dollar per 1/1000 output token: a request reserves about 1_000_000 micros.
    mid = await add_model(db, "openai", "pricey", out="1000")
    await add_route(db, mid)
    await add_budget(db, 1_500_000)
    provider = GatedProvider()
    router_a = make_router(db, {"openai": provider}, prompts)
    router_b = make_router(db, {"openai": provider}, prompts)

    t1 = asyncio.create_task(router_a.complete(request("a", max_out=1000)))
    t2 = asyncio.create_task(router_b.complete(request("b", max_out=1000)))
    await asyncio.wait_for(provider.entered.wait(), 10)
    done, pending = await asyncio.wait({t1, t2}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
    assert len(done) == 1 and len(pending) == 1
    loser = done.pop()
    winner = pending.pop()
    exc = loser.exception()
    assert isinstance(exc, BudgetExhausted)
    assert exc.code.code == "TWR-LLM-001"

    spent, reserved, cap = await counter(db)
    assert spent == 0 and 0 < reserved <= cap  # only the winner holds a reservation
    provider.release.set()
    result = await winner
    assert result.cost_usd_micros == 100_000
    spent, reserved, cap = await counter(db)
    assert (spent, reserved) == (100_000, 0)
    assert provider.calls == 1


async def test_invalid_output_retries_then_falls_back(
    db: Database, prompts: PromptRegistry
) -> None:
    a = await add_model(db, "openai", "model-a")
    b = await add_model(db, "anthropic", "model-b")
    await add_route(db, a, b)
    primary = FakeProvider(
        ProviderResponse({"wrong": 1}, TokenUsage(10, 0, 5), "r1", "end"),
        ProviderInvalidOutput("truncated", usage=TokenUsage(10, 0, 5)),
    )
    fallback = FakeProvider(ok("fixed"))
    router = make_router(db, {"openai": primary, "anthropic": fallback}, prompts)

    result = await router.complete(request())

    assert result.model == "model-b" and isinstance(result.output, Answer)
    assert len(primary.calls) == 2 and len(fallback.calls) == 1
    statuses = await scalar(db, "SELECT array_agg(status ORDER BY id) FROM llm_calls")
    assert statuses == ["invalid_output", "invalid_output", "succeeded"]
    # Every attempt is billed.
    assert await scalar(db, "SELECT sum(cost_usd_micros) FROM llm_calls") == 5 + 5 + 20


async def test_invalid_output_then_valid_on_same_model(
    db: Database, prompts: PromptRegistry
) -> None:
    a = await add_model(db, "openai", "model-a")
    await add_route(db, a)
    provider = FakeProvider(ProviderInvalidOutput("bad json"), ok())
    result = await make_router(db, {"openai": provider}, prompts).complete(request())
    assert result.model == "model-a" and len(provider.calls) == 2


async def test_all_invalid_raises_no_valid_output(db: Database, prompts: PromptRegistry) -> None:
    a = await add_model(db, "openai", "model-a")
    await add_route(db, a)
    provider = FakeProvider(ProviderInvalidOutput("bad"))
    with pytest.raises(NoValidOutput) as info:
        await make_router(db, {"openai": provider}, prompts).complete(request())
    assert info.value.code.code == "TWR-LLM-005"
    assert len(provider.calls) == 2


async def test_refusal_falls_back_and_all_refused(db: Database, prompts: PromptRegistry) -> None:
    a = await add_model(db, "openai", "model-a")
    b = await add_model(db, "anthropic", "model-b")
    await add_route(db, a, b)
    primary = FakeProvider(ProviderRefusal("no"))
    fallback = FakeProvider(ok())
    router = make_router(db, {"openai": primary, "anthropic": fallback}, prompts)
    result = await router.complete(request())
    assert result.model == "model-b"
    assert len(primary.calls) == 1  # refusal: no retry on the same model
    assert await scalar(db, "SELECT count(*) FROM llm_calls WHERE status = 'refused'") == 1

    both = make_router(
        db,
        {
            "openai": FakeProvider(ProviderRefusal("no")),
            "anthropic": FakeProvider(ProviderRefusal("no")),
        },
        prompts,
    )
    with pytest.raises(AllRefused) as info:
        await both.complete(request("different"))
    assert info.value.code.code == "TWR-LLM-004"


async def test_retryable_error_releases_reservation(db: Database, prompts: PromptRegistry) -> None:
    a = await add_model(db, "openai", "model-a")
    b = await add_model(db, "anthropic", "model-b")
    await add_route(db, a, b)
    await add_budget(db, 1_000_000)
    fallback = FakeProvider()
    router = make_router(
        db,
        {"openai": FakeProvider(ProviderRetryable("429", retry_after=7.0)), "anthropic": fallback},
        prompts,
    )
    with pytest.raises(ProviderUnavailable) as info:
        await router.complete(request())
    assert info.value.code.code == "TWR-LLM-006"
    assert info.value.retry_after == 7.0
    assert fallback.calls == []
    assert await counter(db) == (0, 0, 1_000_000)
    assert await scalar(db, "SELECT status FROM llm_calls") == "failed"


async def test_fatal_error_is_surfaced_and_released(db: Database, prompts: PromptRegistry) -> None:
    a = await add_model(db, "openai", "model-a")
    await add_route(db, a)
    await add_budget(db, 1_000_000)
    router = make_router(db, {"openai": FakeProvider(ProviderFatal("bad key"))}, prompts)
    with pytest.raises(ProviderRejected) as info:
        await router.complete(request())
    assert info.value.code.code == "TWR-LLM-007"
    assert await counter(db) == (0, 0, 1_000_000)


async def test_threshold_events_once_per_threshold_and_period(
    db: Database, prompts: PromptRegistry
) -> None:
    mid = await add_model(db, "openai", "model-a")
    await add_route(db, mid)
    await add_budget(db, 1000)

    def provider(out_tokens: int) -> FakeProvider:
        return FakeProvider(ok(usage=TokenUsage(0, 0, out_tokens)))

    async def call(name: str, out_tokens: int) -> None:
        router = make_router(db, {"openai": provider(out_tokens)}, prompts)
        await router.complete(request(name, max_out=10))

    async def thresholds() -> list[int]:
        async with db.session() as session:
            rows = await session.execute(
                text(
                    "SELECT payload->>'threshold_pct' FROM events"
                    " WHERE topic = 'budget.threshold_reached' ORDER BY id"
                )
            )
            return [int(r[0]) for r in rows]

    await call("1", 600)
    assert await thresholds() == [50]
    await call("2", 250)
    assert await thresholds() == [50, 80]
    # Re-crossing 50 and 80 (spent reset by hand) does not publish again.
    async with db.transaction() as session:
        await session.execute(text("UPDATE budget_counters SET spent_usd_micros = 0"))
    await call("3", 900)
    assert await thresholds() == [50, 80]
    await call("4", 200)  # 900 -> 1100: crosses 100 for the first time
    assert await thresholds() == [50, 80, 100]
    async with db.session() as session:
        payload = (
            await session.execute(
                text(
                    "SELECT payload, aggregate_type, aggregate_id, actor FROM events"
                    " WHERE payload->>'threshold_pct' = '50'"
                )
            )
        ).one()
    assert payload[0]["scope"] == "global" and payload[0]["cap_usd_micros"] == 1000
    assert payload[0]["spent_usd_micros"] == 600
    assert tuple(payload[1:]) == ("budget", "global", "system:llm-router")


async def test_budget_exhausted(db: Database, prompts: PromptRegistry) -> None:
    mid = await add_model(db, "openai", "model-a")
    await add_route(db, mid)
    await add_budget(db, 10)  # estimate is 100 output tokens at 1 micro each
    provider = FakeProvider()
    with pytest.raises(BudgetExhausted) as info:
        await make_router(db, {"openai": provider}, prompts).complete(request())
    exc = info.value
    assert exc.code.code == "TWR-LLM-001" and exc.scope == "global"
    assert exc.retry_at == datetime.combine(
        month_period(datetime.now(UTC))[1], datetime.min.time(), UTC
    )
    assert provider.calls == []
    assert await counter(db) == (0, 0, 10)


async def test_scope_refusal_rolls_back_global_reservation(
    db: Database, prompts: PromptRegistry
) -> None:
    mid = await add_model(db, "openai", "model-a")
    await add_route(db, mid)
    await add_budget(db, 1_000_000)
    await add_budget(db, 10, scope="level:N3")
    with pytest.raises(BudgetExhausted) as info:
        await make_router(db, {"openai": FakeProvider()}, prompts).complete(
            request(scope="level:N3")
        )
    assert info.value.scope == "level:N3"
    assert await counter(db) == (0, 0, 1_000_000)
    # A scope without counter is not capped; global still applies and is charged.
    await make_router(db, {"openai": FakeProvider()}, prompts).complete(request("x", scope="surge"))
    assert await counter(db) == (20, 0, 1_000_000)


async def test_both_scopes_are_charged(db: Database, prompts: PromptRegistry) -> None:
    mid = await add_model(db, "openai", "model-a")
    await add_route(db, mid)
    await add_budget(db, 1_000_000)
    await add_budget(db, 1_000_000, scope="level:N3")
    await make_router(db, {"openai": FakeProvider()}, prompts).complete(request(scope="level:N3"))
    assert await counter(db) == (20, 0, 1_000_000)
    assert await counter(db, "level:N3") == (20, 0, 1_000_000)


async def test_missing_global_counter_means_unbudgeted(
    db: Database, prompts: PromptRegistry
) -> None:
    mid = await add_model(db, "openai", "model-a")
    await add_route(db, mid)
    router = make_router(db, {"openai": FakeProvider()}, prompts)
    result = await router.complete(request())
    assert result.cost_usd_micros == 20
    assert await scalar(db, "SELECT count(*) FROM budget_counters") == 0


async def test_level_specific_route_preferred(db: Database, prompts: PromptRegistry) -> None:
    generic = await add_model(db, "openai", "generic")
    special = await add_model(db, "openai", "special")
    await add_route(db, generic, level="*")
    await add_route(db, special, level="N4")
    provider = FakeProvider()
    router = make_router(db, {"openai": provider}, prompts)
    await router.complete(request("a", level=Level.N4))
    await router.complete(request("b", level=Level.N1))
    assert provider.models == ["special", "generic"]


async def test_route_and_prompt_errors(db: Database, prompts: PromptRegistry) -> None:
    router = make_router(db, {"openai": FakeProvider()}, prompts)
    with pytest.raises(NoRoute) as no_route:
        await router.complete(request())
    assert no_route.value.code.code == "TWR-LLM-002"

    mid = await add_model(db, "openai", "model-a")
    await add_route(db, mid)
    bad = LLMRequest("extract", Level.N1, "nope", {}, Answer, 10)
    with pytest.raises(UnknownPrompt) as unknown_prompt:
        await router.complete(bad)
    assert unknown_prompt.value.code.code == "TWR-LLM-003"

    async with db.transaction() as session:
        await session.execute(text("UPDATE llm_models SET active = false"))
    with pytest.raises(UnknownModel) as unknown_model:
        await router.complete(request())
    assert unknown_model.value.code.code == "TWR-LLM-009"


async def test_month_rollover_creates_counter_from_previous_cap(
    db: Database, prompts: PromptRegistry
) -> None:
    mid = await add_model(db, "openai", "model-a")
    await add_route(db, mid)
    await add_budget(db, 5_000_000, spent=123, start=date(2026, 11, 1))
    clock = FakeClock(datetime(2026, 12, 5, 10, tzinfo=UTC))
    router = make_router(db, {"openai": FakeProvider()}, prompts, clock=clock)

    await router.complete(request())

    async with db.session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT period_start, period_end, cap_usd_micros, spent_usd_micros,"
                    " reserved_usd_micros FROM budget_counters ORDER BY period_start"
                )
            )
        ).all()
    assert [tuple(r) for r in rows] == [
        (date(2026, 11, 1), date(2026, 12, 1), 5_000_000, 123, 0),
        (date(2026, 12, 1), date(2027, 1, 1), 5_000_000, 20, 0),
    ]


def test_to_job_error_mapping() -> None:
    future = datetime(2999, 1, 1, tzinfo=UTC)
    retry = to_job_error(BudgetExhausted("global", future))
    assert isinstance(retry, RetryLater) and retry.delay > 3600
    soon = to_job_error(BudgetExhausted("global", datetime.now(UTC)))
    assert isinstance(soon, RetryLater) and soon.delay == 60

    unavailable = to_job_error(ProviderUnavailable("x", retry_after=12.0))
    assert isinstance(unavailable, RetryLater) and unavailable.delay == 12.0
    default = to_job_error(ProviderUnavailable("x"))
    assert isinstance(default, RetryLater) and default.delay == 60

    for permanent in (
        NoRoute("a"),
        UnknownPrompt("b"),
        UnknownModel("c"),
        ProviderNotConfigured("d"),
        ProviderRejected("e"),
        AllRefused("f"),
    ):
        assert isinstance(to_job_error(permanent), PermanentError)

    invalid = NoValidOutput("g")
    assert to_job_error(invalid) is invalid
    assert isinstance(invalid, LLMRouterError)


async def test_seed_is_idempotent_and_keeps_modified_routes(
    db: Database, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    await run_seed(settings)
    first = capsys.readouterr().out
    assert "Models upserted: 12" in first and "Routes created: 15" in first

    async def counts() -> tuple[int, int, int]:
        return (
            await scalar(db, "SELECT count(*) FROM llm_models"),
            await scalar(db, "SELECT count(*) FROM llm_routes"),
            await scalar(db, "SELECT count(*) FROM budget_counters"),
        )

    assert await counts() == (12, 15, 2)
    assert (
        await scalar(db, "SELECT cap_usd_micros FROM budget_counters WHERE scope = 'global'")
        == 30_000_000
    )
    assert (
        await scalar(db, "SELECT free_group FROM llm_models WHERE model = 'gpt-5.6-sol'") == "large"
    )
    assert await scalar(db, "SELECT free_group IS NULL FROM llm_models WHERE model = 'gpt-6-luna'")
    assert (
        await scalar(
            db,
            "SELECT params->>'dimensions' FROM llm_routes WHERE role = 'embed' AND level = '*'",
        )
        == "512"
    )

    async with db.transaction() as session:
        await session.execute(
            text(
                'UPDATE llm_routes SET params = CAST(\'{"effort": "high"}\' AS jsonb)'
                " WHERE role = 'extract'"
            )
        )
        await session.execute(text("UPDATE llm_models SET input_usd_per_mtok = 99"))
        await session.execute(text("UPDATE budget_counters SET cap_usd_micros = 1"))
    await run_seed(settings)
    second = capsys.readouterr().out
    assert "Routes created: 0 (kept: 15)" in second
    assert await counts() == (12, 15, 2)
    assert (
        await scalar(db, "SELECT params->>'effort' FROM llm_routes WHERE role = 'extract'")
        == "high"
    )
    assert (
        await scalar(db, "SELECT cap_usd_micros FROM budget_counters WHERE scope = 'global'") == 1
    )
    assert await scalar(
        db, "SELECT input_usd_per_mtok FROM llm_models WHERE model = 'gpt-5-nano'"
    ) == Decimal("0.05")


async def test_run_usage_prints(
    db: Database, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    await run_seed(settings)
    async with db.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO llm_free_usage (provider, free_group, day, tokens)"
                " VALUES ('openai', 'small', (now() AT TIME ZONE 'UTC')::date, 250000)"
            )
        )
    capsys.readouterr()
    await run_usage(settings)
    out = capsys.readouterr().out
    assert "openai/small" in out and "10.0%" in out
    assert "openai/large" in out
    assert "global" in out and "$30.00" in out
