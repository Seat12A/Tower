"""Embeddings: ``FakeEmbedder`` properties and ``EmbeddingService`` (cache, budget,
journal, dimension check, provider failures). Real PostgreSQL, no network."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from tower.core.adapters.fake_llm import FakeEmbedder
from tower.core.db.database import Database
from tower.core.domain.llm import month_period
from tower.core.factcheck import runtime
from tower.core.llm.admin import run_seed
from tower.core.llm.embeddings import EmbeddingService
from tower.core.llm.errors import BudgetExhausted, NoRoute
from tower.core.ports.llm import ProviderFatal, ProviderRetryable

from .conftest import make_settings

DIMS = 512


def cos(a: Sequence[float], b: Sequence[float]) -> float:
    return math.fsum(x * y for x, y in zip(a, b, strict=True))


class ScriptedEmbedder:
    model = "text-embedding-3-small"
    version = "text-embedding-3-small"

    def __init__(self, *, error: Exception | None = None, dims: int = DIMS, scale: float = 3.0):
        self.calls: list[list[str]] = []
        self.error = error
        self.dims = dims
        self.scale = scale

    async def embed(self, texts: Sequence[str], *, dimensions: int) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.error is not None:
            raise self.error
        return [[self.scale] + [0.0] * (self.dims - 1) for _ in texts]


@pytest.fixture(autouse=True)
def _reset_runtime() -> None:
    runtime.clear_overrides()


@pytest.fixture
async def seeded(db: Database) -> None:
    await run_seed(make_settings(llm_fake=True))


async def scalar(db: Database, sql: str, **params: object) -> object:
    async with db.session() as s:
        return (await s.execute(text(sql), params)).scalar_one()


# ----------------------------------------------------------------------- FakeEmbedder


async def test_fake_embedder_similarity_properties() -> None:
    fake = FakeEmbedder()
    base, near, far, empty, empty2 = await fake.embed(
        [
            "Le chômage est passé à 7,5 % en France cette année",
            "Le chômage est passé à 8,5 % en France cette année",
            "Les pandas géants mangent du bambou dans les montagnes",
            "",
            "   ",
        ],
        dimensions=DIMS,
    )
    assert all(len(v) == DIMS for v in (base, near, far, empty))
    assert math.isclose(cos(base, base), 1.0, abs_tol=1e-9)
    assert cos(base, near) > 0.8
    assert cos(base, far) < 0.3
    assert empty == empty2 and math.isclose(cos(empty, empty), 1.0)
    assert (await fake.embed(["Éléphant"], dimensions=DIMS)) == await fake.embed(
        ["elephant"], dimensions=DIMS
    )
    assert fake.model == fake.version == "fake-embedding-512"
    with pytest.raises(ValueError, match="512"):
        await fake.embed(["x"], dimensions=64)


# ------------------------------------------------------------------------- the service


async def test_service_returns_normalized_vectors_and_journals_a_call(
    db: Database, seeded: None
) -> None:
    emb = ScriptedEmbedder()
    service = EmbeddingService(db, emb)
    result = await service.embed(["a" * 300, "b"], level="N1", ref="t")
    assert result.model == "text-embedding-3-small" and result.cache_hits == 0
    assert result.vectors[0][0] == pytest.approx(1.0) and len(result.vectors[0]) == DIMS
    assert result.cost_usd_micros >= 1
    assert await scalar(db, "SELECT count(*) FROM llm_calls WHERE role = 'embed'") == 1
    call = await scalar(
        db,
        "SELECT concat_ws('|', provider, model, status, budget_scope, input_tokens > 0,"
        " output IS NULL, length(cache_key)) FROM llm_calls WHERE role = 'embed'",
    )
    assert call == "openai|text-embedding-3-small|succeeded|level:N1|t|t|64"
    start, _ = month_period(datetime.now(UTC))
    spent = await scalar(
        db,
        "SELECT spent_usd_micros FROM budget_counters WHERE scope = 'global' AND period_start = :p",
        p=start,
    )
    assert spent == result.cost_usd_micros
    level_spent = await scalar(
        db,
        "SELECT spent_usd_micros FROM budget_counters"
        " WHERE scope = 'level:N1' AND period_start = :p",
        p=start,
    )
    assert level_spent == result.cost_usd_micros
    assert await scalar(db, "SELECT count(*) FROM budget_reservations") == 0


async def test_cache_hits_are_neither_sent_nor_charged(db: Database, seeded: None) -> None:
    emb = ScriptedEmbedder()
    service = EmbeddingService(db, emb)
    first = await service.embed(["one", "two"], level="N1", ref="t")
    second = await service.embed(["two", "three", "one", "two"], level="N1", ref="t")
    assert second.cache_hits == 3
    assert emb.calls == [["one", "two"], ["three"]]
    assert second.vectors[0] == first.vectors[1] and second.vectors[1] is not second.vectors[2]
    only_cached = await service.embed(["one"], level="N1", ref="t")
    assert only_cached.cost_usd_micros == 0 and only_cached.cache_hits == 1
    assert len(emb.calls) == 2
    assert await scalar(db, "SELECT count(*) FROM llm_calls WHERE role = 'embed'") == 2


async def test_cache_is_bounded(db: Database, seeded: None) -> None:
    emb = ScriptedEmbedder()
    service = EmbeddingService(db, emb, cache_size=2)
    await service.embed(["a", "b", "c"], level="N1", ref="t")
    assert len(service._cache) == 2
    again = await service.embed(["a"], level="N1", ref="t")  # evicted: sent again
    assert again.cache_hits == 0


async def test_batches_of_256_one_journal_row_each(db: Database, seeded: None) -> None:
    emb = ScriptedEmbedder()
    service = EmbeddingService(db, emb)
    texts = [f"text {i}" for i in range(300)]
    result = await service.embed(texts, level="N1", ref="t")
    assert [len(c) for c in emb.calls] == [256, 44] and len(result.vectors) == 300
    assert await scalar(db, "SELECT count(*) FROM llm_calls WHERE role = 'embed'") == 2


async def test_identical_batch_is_still_journalled(db: Database, seeded: None) -> None:
    emb = ScriptedEmbedder()
    await EmbeddingService(db, emb).embed(["same"], level="N1", ref="t")
    await EmbeddingService(db, emb).embed(["same"], level="N1", ref="t")  # new process cache
    assert await scalar(db, "SELECT count(*) FROM llm_calls WHERE role = 'embed'") == 2
    assert await scalar(db, "SELECT count(DISTINCT cache_key) FROM llm_calls") == 2


async def test_empty_input_does_nothing(db: Database) -> None:
    result = await EmbeddingService(db, ScriptedEmbedder()).embed([], level="N1", ref="t")
    assert result.vectors == [] and result.cost_usd_micros == 0


async def test_budget_exhausted_raises_and_releases(db: Database, seeded: None) -> None:
    start, _ = month_period(datetime.now(UTC))
    async with db.transaction() as s:
        await s.execute(
            text("UPDATE budget_counters SET cap_usd_micros = 0 WHERE scope = 'level:N1'")
        )
    emb = ScriptedEmbedder()
    with pytest.raises(BudgetExhausted) as info:
        await EmbeddingService(db, emb).embed(["hello"], level="N1", ref="t")
    assert info.value.retry_at.date() > start
    assert emb.calls == []
    assert await scalar(db, "SELECT count(*) FROM budget_reservations") == 0
    assert await scalar(db, "SELECT count(*) FROM llm_calls") == 0


async def test_provider_failure_is_captured_and_retryable(db: Database, seeded: None) -> None:
    emb = ScriptedEmbedder(error=ProviderRetryable("boom", retry_after=12.0))
    with pytest.raises(ProviderRetryable) as info:
        await EmbeddingService(db, emb).embed(["hello"], level="N1", ref="t")
    assert info.value.retry_after == 12.0
    assert await scalar(db, "SELECT count(*) FROM budget_reservations") == 0
    row = await scalar(
        db, "SELECT concat_ws('|', status, cost_usd_micros) FROM llm_calls WHERE role = 'embed'"
    )
    assert row == "failed|0"
    spent = await scalar(db, "SELECT spent_usd_micros FROM budget_counters WHERE scope = 'global'")
    assert spent == 0
    async with db.session() as s:
        codes = (await s.execute(text("SELECT code FROM error_reports"))).scalars().all()
    # the reporter is flushed lazily in tests: the capture call itself must not raise
    assert set(codes) <= {"TWR-FCK-033"}


async def test_dimension_mismatch_is_rejected(db: Database, seeded: None) -> None:
    emb = ScriptedEmbedder(dims=100)
    with pytest.raises(ProviderFatal):
        await EmbeddingService(db, emb).embed(["hello"], level="N1", ref="t")
    assert await scalar(db, "SELECT count(*) FROM llm_calls WHERE status = 'failed'") == 1


async def test_unexpected_error_releases_the_reservation(db: Database, seeded: None) -> None:
    emb = ScriptedEmbedder(error=RuntimeError("bug"))
    with pytest.raises(RuntimeError):
        await EmbeddingService(db, emb).embed(["hello"], level="N1", ref="t")
    assert await scalar(db, "SELECT count(*) FROM budget_reservations") == 0


async def test_no_route(db: Database) -> None:
    with pytest.raises(NoRoute):
        await EmbeddingService(db, ScriptedEmbedder()).embed(["x"], level="N1", ref="t")


async def test_runtime_get_embedder(db: Database) -> None:
    settings = make_settings(llm_fake=True)
    service = runtime.get_embedder(settings, db)
    assert service is runtime.get_embedder(settings, db)
    assert service.model_name == "fake-embedding-512"
    runtime.set_override("embedder", "x")
    assert runtime.get_embedder(settings, db) == "x"  # type: ignore[comparison-overlap]
