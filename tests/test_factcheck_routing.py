"""E1 routing: subscription, embedding batch job, ``record_routing``, router models and
weekly partitions. Real PostgreSQL; embeddings from ``FakeEmbedder``."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
import structlog
from sqlalchemy import text

from tower.core.adapters.fake_llm import FakeEmbedder
from tower.core.db.database import Database
from tower.core.domain.errors import InvalidInput, NotFound, PermissionDenied
from tower.core.domain.ingestion import AuthorInput, ItemInput
from tower.core.domain.security import Actor, ActorKind
from tower.core.events.outbox import EventRecord
from tower.core.factcheck import names, runtime
from tower.core.factcheck.profiles import invalidate_profile_cache
from tower.core.jobs.queue import JobRecord, RetryLater
from tower.core.llm.admin import run_seed
from tower.core.llm.embeddings import EmbeddingService
from tower.core.services.base import JobContext
from tower.core.services.registry import load_services
from tower.core.usecases import ingestion as ing
from tower.core.usecases import watchlist as watch_uc
from tower.core.usecases.routing import (
    RoutingRow,
    activate_router_model,
    create_router_model,
    record_routing,
)
from tower.services.factcheck import routing as routing_service
from tower.services.factcheck.routing import (
    embed_batch,
    embedding_partitions,
    on_content_to_embed,
)

from .conftest import make_settings

SYSTEM = Actor.system("test")
NOW = datetime.now(UTC)
WATCHED = "7000"
OTHER = "7001"
POSITIVE = "Le chômage est passé à 7,5 % en France selon l'INSEE"
UNRELATED = "Les pandas géants mangent du bambou dans les montagnes"
MODEL = "fake-embedding-512"


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    runtime.clear_overrides()
    invalidate_profile_cache()
    monkeypatch.setattr(routing_service, "_model_cache", None)
    yield
    runtime.clear_overrides()
    invalidate_profile_cache()


@pytest.fixture
async def seeded(db: Database) -> FakeEmbedder:
    await run_seed(make_settings(llm_fake=True))
    fake = FakeEmbedder()
    runtime.set_override("embedder", EmbeddingService(db, fake))
    return fake


async def scalar(db: Database, sql: str, **params: Any) -> Any:
    async with db.session() as s:
        return (await s.execute(text(sql), params)).scalar_one()


async def rows(db: Database, sql: str, **params: Any) -> list[Any]:
    async with db.session() as s:
        return list((await s.execute(text(sql), params)).all())


async def make_contents(
    db: Database, items: list[tuple[str, str, str | None, str]]
) -> dict[str, int]:
    """``(external_id, text, author, lang)`` -> ``{external_id: content id}``."""
    async with db.transaction() as session:
        await ing.ingest_items(
            session,
            SYSTEM,
            [
                ItemInput(
                    platform="x",
                    external_id=ext,
                    text=text_,
                    author=AuthorInput(external_id=author, handle="someone") if author else None,
                    lang_hint=lang,
                    created_at=NOW - timedelta(hours=1),
                    metrics={},
                )
                for ext, text_, author, lang in items
            ],
            source="radar",
            filter_version=None,
            installation_token=None,
            max_items=1000,
            daily_quota=1000,
        )
    found = await rows(db, "SELECT external_id, id FROM content_items")
    return {r[0]: int(r[1]) for r in found}


async def watch(db: Database, ext: str = WATCHED) -> None:
    async with db.transaction() as s:
        await watch_uc.add_watched_account(
            s, SYSTEM, platform="x", external_id=ext, category="media_outlet"
        )


def ctx(db: Database, settings: Any, job_id: int = 1) -> JobContext:
    job = JobRecord(
        id=job_id,
        queue=names.FC_BULK,
        kind=names.JOB_EMBED_BATCH,
        payload={},
        priority=0,
        attempts=1,
        max_attempts=3,
        dedupe_key=None,
        created_at=NOW,
    )
    return JobContext(job=job, db=db, settings=settings, log=structlog.get_logger("test"))


def event(lang: str, id_: int = 1) -> EventRecord:
    return EventRecord(
        id=id_,
        xid=1,
        topic=names.CONTENT_INGESTED,
        aggregate_type="content",
        aggregate_id="x",
        payload={"id": str(id_), "lang": lang},
        actor=None,
        created_at=NOW,
    )


class Constant:
    """Replaces the sampling RNG."""

    def __init__(self, value: float) -> None:
        self.value = value

    def random(self) -> float:
        return self.value


async def activate_model(db: Database, *, model: str = MODEL, recall: float = 0.95) -> int:
    """A router model scoring high for texts close to ``POSITIVE``, activated."""
    (v,) = await FakeEmbedder().embed([POSITIVE], dimensions=512)
    async with db.transaction() as s:
        row = await create_router_model(
            s,
            SYSTEM,
            embedding_model=model,
            weights=[20.0 * x for x in v],
            bias=-10.0,
            metrics={"recall": recall},
            sample_size=300,
        )
        await activate_router_model(
            s, SYSTEM, version=row.version, expected_model=model, force=True, reason="test"
        )
        return row.version


# ------------------------------------------------------------------- subscription


async def test_subscription_is_registered() -> None:
    registered = load_services(["factcheck"])
    embed = [sub for name, sub in registered.subscriptions if name == "factcheck.embed"]
    assert embed and embed[0].topics == (names.CONTENT_INGESTED,)
    assert registered.handlers[names.JOB_EMBED_BATCH].queue == names.FC_BULK
    assert any(p.name == "factcheck:embedding_partitions" for p in registered.periodic)


async def test_subscription_filters_language_and_collapses_per_minute(db: Database) -> None:
    async with db.transaction() as s:
        await on_content_to_embed(s, [event("de", 1), event("zz", 2)])
    assert await scalar(db, "SELECT count(*) FROM jobs") == 0
    async with db.transaction() as s:
        await on_content_to_embed(s, [event("fr", 1), event("de", 2), event("fr", 3)])
    async with db.transaction() as s:
        await on_content_to_embed(s, [event("fr", 4)])
    found = await rows(db, "SELECT queue, kind, payload, dedupe_key, run_at > now() FROM jobs")
    assert len(found) == 1
    queue, kind, payload, key, delayed = found[0]
    assert (queue, kind, payload) == (names.FC_BULK, names.JOB_EMBED_BATCH, {})
    assert key.startswith("emb:") and len(key) == 4 + 12
    assert delayed is True


# ---------------------------------------------------------------------------- job


async def test_cold_start_is_permissive_and_budgets_by_level(
    db: Database, seeded: FakeEmbedder
) -> None:
    await watch(db)
    ids = await make_contents(
        db,
        [
            ("100", POSITIVE, WATCHED, "fr"),
            ("101", UNRELATED, OTHER, "fr"),
            ("102", "A copy of something", OTHER, "fr"),
            ("103", "Ein deutscher Satz", OTHER, "de"),
            ("104", "An old post", OTHER, "fr"),
        ],
    )
    async with db.transaction() as s:
        await s.execute(
            text("UPDATE content_items SET copy_of_id = :o WHERE id = :c"),
            {"o": ids["100"], "c": ids["102"]},
        )
        await s.execute(
            text(
                "UPDATE content_items SET first_seen_at = now() - interval '40 days' WHERE id = :c"
            ),
            {"c": ids["104"]},
        )
    await embed_batch(ctx(db, make_settings(llm_fake=True)))
    found = await rows(
        db,
        "SELECT content_id, score, level, sampled, router_version, model, lang"
        " FROM content_embeddings ORDER BY content_id",
    )
    assert [r[0] for r in found] == [ids["100"], ids["101"]]
    assert all(r[1] is None and r[2] == "N1" and not r[3] and r[4] is None for r in found)
    assert all(r[5] == MODEL and r[6] == "fr" for r in found)
    scopes = await rows(
        db, "SELECT budget_scope FROM llm_calls WHERE role = 'embed' ORDER BY budget_scope"
    )
    assert [r[0] for r in scopes] == ["level:N1", "level:N3"]
    assert (
        await scalar(db, "SELECT count(*) FROM jobs WHERE kind = :k", k=names.JOB_EMBED_BATCH) == 0
    )
    # the vector round-trips through halfvec
    dims = await scalar(db, "SELECT vector_dims(embedding) FROM content_embeddings LIMIT 1")
    assert dims == 512


async def test_second_run_is_idempotent(db: Database, seeded: FakeEmbedder) -> None:
    await make_contents(db, [("110", POSITIVE, OTHER, "fr"), ("111", UNRELATED, OTHER, "fr")])
    settings = make_settings(llm_fake=True)
    await embed_batch(ctx(db, settings))
    calls = await scalar(db, "SELECT count(*) FROM llm_calls")
    await embed_batch(ctx(db, settings, job_id=2))
    assert await scalar(db, "SELECT count(*) FROM content_embeddings") == 2
    assert await scalar(db, "SELECT count(*) FROM llm_calls") == calls


async def test_active_model_scores_discards_and_samples(
    db: Database, seeded: FakeEmbedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    version = await activate_model(db)
    ids = await make_contents(db, [("120", POSITIVE, OTHER, "fr"), ("121", UNRELATED, OTHER, "fr")])
    monkeypatch.setattr(routing_service, "_rng", Constant(0.0))  # always sampled
    await embed_batch(ctx(db, make_settings(llm_fake=True)))
    got = {
        r[0]: r
        for r in await rows(
            db, "SELECT content_id, score, level, sampled, router_version FROM content_embeddings"
        )
    }
    assert got[ids["120"]][1] > 0.9 and got[ids["120"]][2:] == ("N1", False, version)
    assert got[ids["121"]][1] < 0.15 and got[ids["121"]][2:] == ("N0", True, version)


async def test_discarded_content_is_not_sampled_when_the_draw_is_high(
    db: Database, seeded: FakeEmbedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    await activate_model(db)
    ids = await make_contents(db, [("130", UNRELATED, OTHER, "fr")])
    monkeypatch.setattr(routing_service, "_rng", Constant(0.99))
    await embed_batch(ctx(db, make_settings(llm_fake=True)))
    row = (await rows(db, "SELECT level, sampled FROM content_embeddings"))[0]
    assert tuple(row) == ("N0", False) and ids


async def test_model_of_another_embedding_model_is_ignored(
    db: Database, seeded: FakeEmbedder
) -> None:
    await activate_model(db, model="other-model")
    await make_contents(db, [("140", UNRELATED, OTHER, "fr")])
    await embed_batch(ctx(db, make_settings(llm_fake=True)))
    row = (await rows(db, "SELECT score, level, router_version FROM content_embeddings"))[0]
    assert tuple(row) == (None, "N1", None)


async def test_continuation_job_when_contents_remain(db: Database, seeded: FakeEmbedder) -> None:
    await make_contents(
        db, [(str(200 + i), f"Post number {i} about taxes", OTHER, "fr") for i in range(105)]
    )
    settings = make_settings(llm_fake=True, fc_embed_batch_size=100)
    await embed_batch(ctx(db, settings, job_id=9))
    assert await scalar(db, "SELECT count(*) FROM content_embeddings") == 100
    queued = await rows(
        db, "SELECT dedupe_key, run_at <= now() FROM jobs WHERE kind = :k", k=names.JOB_EMBED_BATCH
    )
    assert [tuple(r) for r in queued] == [("emb:next9", True)]
    await embed_batch(ctx(db, settings, job_id=10))
    assert await scalar(db, "SELECT count(*) FROM content_embeddings") == 105
    assert (
        await scalar(db, "SELECT count(*) FROM jobs WHERE kind = :k", k=names.JOB_EMBED_BATCH) == 1
    )


async def test_budget_exhausted_retries_later_and_keeps_the_other_level(
    db: Database, seeded: FakeEmbedder
) -> None:
    await watch(db)
    ids = await make_contents(
        db, [("300", POSITIVE, WATCHED, "fr"), ("301", UNRELATED, OTHER, "fr")]
    )
    async with db.transaction() as s:
        await s.execute(
            text("UPDATE budget_counters SET cap_usd_micros = 0 WHERE scope = 'level:N3'")
        )
    with pytest.raises(RetryLater) as info:
        await embed_batch(ctx(db, make_settings(llm_fake=True)))
    assert info.value.delay >= 60
    done = await rows(db, "SELECT content_id FROM content_embeddings")
    assert [r[0] for r in done] == [ids["301"]]


# ----------------------------------------------------------------- use cases


async def row_for(content_id: int, level: str = "N1") -> RoutingRow:
    (v,) = await FakeEmbedder().embed(["x"], dimensions=512)
    return RoutingRow(content_id, v, MODEL, "fr", None, level)


async def test_record_routing_checks_permission_input_and_duplicates(
    db: Database, seeded: FakeEmbedder
) -> None:
    ids = await make_contents(db, [("400", "Some post", OTHER, "fr")])
    cid = ids["400"]
    anonymous = Actor(ActorKind.ANONYMOUS)
    async with db.transaction() as s:
        with pytest.raises(PermissionDenied):
            await record_routing(s, anonymous, [await row_for(cid)])
    async with db.transaction() as s:
        with pytest.raises(InvalidInput):
            await record_routing(s, SYSTEM, [await row_for(cid, level="N3")])
    async with db.transaction() as s:
        bad = RoutingRow(cid, [0.1, 0.2], MODEL, "fr", None, "N1")
        with pytest.raises(InvalidInput):
            await record_routing(s, SYSTEM, [bad])
    async with db.transaction() as s:
        assert await record_routing(s, SYSTEM, []) == 0
        assert await record_routing(s, SYSTEM, [await row_for(cid)]) == 1
    async with db.transaction() as s:
        assert await record_routing(s, SYSTEM, [await row_for(cid)]) == 0
    assert await scalar(db, "SELECT count(*) FROM content_embeddings") == 1
    assert await scalar(db, "SELECT count(*) FROM events WHERE topic LIKE 'content_emb%'") == 0


async def test_router_model_versions_and_activation_rules(db: Database) -> None:
    weights = [0.1] * 512
    async with db.transaction() as s:
        a = await create_router_model(
            s,
            SYSTEM,
            embedding_model=MODEL,
            weights=weights,
            bias=0.0,
            metrics={"recall": 0.95, "distillation": {"meets_criterion": True}},
            sample_size=10,
        )
        b = await create_router_model(
            s,
            SYSTEM,
            embedding_model=MODEL,
            weights=weights,
            bias=0.0,
            metrics={"recall": 0.5},
            sample_size=10,
        )
        assert (a.version, b.version, a.active, b.active) == (1, 2, False, False)
        with pytest.raises(InvalidInput):
            await create_router_model(
                s,
                SYSTEM,
                embedding_model=MODEL,
                weights=[0.1] * 3,
                bias=0.0,
                metrics={},
                sample_size=1,
            )
        with pytest.raises(InvalidInput):
            await create_router_model(
                s,
                SYSTEM,
                embedding_model=MODEL,
                weights=[float("nan")] * 512,
                bias=0.0,
                metrics={},
                sample_size=1,
            )
        with pytest.raises(PermissionDenied):
            await create_router_model(
                s,
                Actor(ActorKind.ANONYMOUS),
                embedding_model=MODEL,
                weights=weights,
                bias=0.0,
                metrics={},
                sample_size=1,
            )
    async with db.transaction() as s:
        await activate_router_model(s, SYSTEM, version=1, expected_model=MODEL, reason="ok")
    async with db.transaction() as s:
        with pytest.raises(InvalidInput):  # distillation not measured, recall 0.5 < 0.90
            await activate_router_model(s, SYSTEM, version=2, expected_model=MODEL, reason="x")
    async with db.transaction() as s:
        with pytest.raises(InvalidInput):  # model mismatch, even with force
            await activate_router_model(
                s, SYSTEM, version=2, expected_model="other", force=True, reason="x"
            )
    async with db.transaction() as s:
        with pytest.raises(NotFound):
            await activate_router_model(s, SYSTEM, version=9, expected_model=MODEL, reason="x")
    assert await scalar(db, "SELECT version FROM router_models WHERE active") == 1
    async with db.transaction() as s:
        await activate_router_model(
            s, SYSTEM, version=2, expected_model=MODEL, force=True, reason="x"
        )
    actives = await rows(db, "SELECT version, activated_by FROM router_models WHERE active")
    assert [tuple(r) for r in actives] == [(2, "system:test")]


# ------------------------------------------------------------------- partitions


def monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


async def partitions(db: Database) -> set[str]:
    found = await rows(
        db,
        "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid"
        " WHERE i.inhparent = 'content_embeddings'::regclass",
    )
    return {r[0] for r in found}


async def test_partitions_are_created_and_expired(db: Database) -> None:
    settings = make_settings(fc_embedding_retention_days=14)
    this_week = monday(datetime.now(UTC).date())
    old = this_week - timedelta(days=7 * 5)
    recent = this_week - timedelta(days=7)
    async with db.transaction() as s:
        for week in (old, recent):
            await s.execute(
                text(
                    f'CREATE TABLE IF NOT EXISTS "content_embeddings_p{week:%Y%m%d}" '
                    f"PARTITION OF content_embeddings FOR VALUES FROM ('{week} 00:00+00') "
                    f"TO ('{week + timedelta(days=7)} 00:00+00')"
                )
            )
        # make the next partitions disappear to prove they are created
        for k in (1, 2):
            week = this_week + timedelta(days=7 * k)
            await s.execute(text(f'DROP TABLE IF EXISTS "content_embeddings_p{week:%Y%m%d}"'))
    async with db.transaction() as s:
        changed = await embedding_partitions(s, settings)
    found = await partitions(db)
    expected = {f"content_embeddings_p{this_week + timedelta(days=7 * k):%Y%m%d}" for k in range(3)}
    assert expected <= found
    assert f"content_embeddings_p{old:%Y%m%d}" not in found
    assert f"content_embeddings_p{recent:%Y%m%d}" in found
    assert changed >= 3
    async with db.transaction() as s:
        assert await embedding_partitions(s, settings) == 0


async def test_activation_requires_the_distillation_criterion(db: Database) -> None:
    async with db.transaction() as s:
        for met in (False, True):
            await create_router_model(
                s,
                SYSTEM,
                embedding_model=MODEL,
                weights=[0.1] * 512,
                bias=0.0,
                metrics={"recall": 0.99, "distillation": {"meets_criterion": met}},
                sample_size=300,
            )
    async with db.transaction() as s:
        with pytest.raises(InvalidInput, match="Distillation criterion"):
            await activate_router_model(s, SYSTEM, version=1, expected_model=MODEL, reason="x")
    async with db.transaction() as s:
        await activate_router_model(s, SYSTEM, version=2, expected_model=MODEL, reason="ok")
    assert await scalar(db, "SELECT version FROM router_models WHERE active") == 2
