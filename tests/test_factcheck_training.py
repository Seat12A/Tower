"""E1 router training and activation (``tower factcheck train-router`` /
``activate-router``) end to end on a small synthetic database."""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from tower.cli import build_parser
from tower.core.db.database import Database
from tower.core.domain.ingestion import AuthorInput, ItemInput
from tower.core.domain.security import Actor
from tower.core.factcheck import names
from tower.core.factcheck.profiles import invalidate_profile_cache
from tower.core.factcheck.training import (
    EXIT_NOT_ENOUGH_SAMPLES,
    load_samples,
    run_activate,
    run_train,
)
from tower.core.usecases import ingestion as ing
from tower.core.usecases.routing import RoutingRow, create_router_model, record_routing

from .conftest import make_settings

SYSTEM = Actor.system("test")
NOW = datetime.now(UTC)
MODEL = "fake-embedding-512"
DIMS = 512


@pytest.fixture(autouse=True)
def _reset() -> None:
    invalidate_profile_cache()


async def scalar(db: Database, sql: str, **params: Any) -> Any:
    async with db.session() as s:
        return (await s.execute(text(sql), params)).scalar_one()


def vector(label: int, rng: random.Random) -> list[float]:
    """Separable by coordinate 0 (sign = label), noise elsewhere, L2-normalized."""
    v = [rng.gauss(0, 0.15) for _ in range(DIMS)]
    v[0] = 1.0 if label else -1.0
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


async def make_contents(db: Database, count: int, start: int = 1000) -> list[int]:
    async with db.transaction() as session:
        await ing.ingest_items(
            session,
            SYSTEM,
            [
                ItemInput(
                    platform="x",
                    external_id=str(start + i),
                    text=f"Post {i}",
                    author=AuthorInput(external_id="9000", handle="someone"),
                    lang_hint="fr",
                    created_at=NOW - timedelta(hours=1),
                    metrics={},
                )
                for i in range(count)
            ],
            source="radar",
            filter_version=None,
            installation_token=None,
            max_items=count + 10,
            daily_quota=count + 10,
        )
    async with db.session() as s:
        result = await s.execute(text("SELECT id FROM content_items ORDER BY id"))
        return [int(r[0]) for r in result]


async def add_claim(db: Database, content_id: int, *, verifiability: float, kind: str) -> int:
    async with db.transaction() as s:
        claim_id = (
            await s.execute(
                text(
                    "INSERT INTO claims (lang_original, text_original, text_fr, claim_type,"
                    " verifiability, prompt_version) VALUES ('fr', 'c', 'c', :k, :v, 'p')"
                    " RETURNING id"
                ),
                {"k": kind, "v": verifiability},
            )
        ).scalar_one()
        await s.execute(
            text("INSERT INTO content_claims (content_id, claim_id) VALUES (:c, :i)"),
            {"c": content_id, "i": claim_id},
        )
    return int(claim_id)


async def add_extract_job(db: Database, content_id: int, *, status: str = "succeeded") -> None:
    async with db.transaction() as s:
        await s.execute(
            text(
                "INSERT INTO jobs (queue, kind, status, dedupe_key) VALUES ('fc-fast', :k, :s, :d)"
            ),
            {"k": names.JOB_EXTRACT, "s": status, "d": names.extract_key(content_id, "N3")},
        )


async def embed_rows(db: Database, vectors: dict[int, list[float]]) -> None:
    async with db.transaction() as s:
        await record_routing(
            s,
            SYSTEM,
            [RoutingRow(cid, v, MODEL, "fr", None, "N1") for cid, v in vectors.items()],
        )


async def build_dataset(db: Database, per_class: int = 120, seed: int = 4) -> None:
    rng = random.Random(seed)  # noqa: S311
    ids = await make_contents(db, per_class * 2)
    vectors: dict[int, list[float]] = {}
    for i, cid in enumerate(ids):
        label = i % 2
        vectors[cid] = vector(label, rng)
        if label:
            await add_claim(db, cid, verifiability=0.9, kind="statistic")
        else:
            await add_extract_job(db, cid)
    await embed_rows(db, vectors)


# ------------------------------------------------------------------------- labels


async def test_labels_distil_e2_and_reviewers(db: Database) -> None:
    ids = await make_contents(db, 7)
    a, b, c, d, _e, f, g = ids
    rng = random.Random(0)  # noqa: S311
    await embed_rows(db, {cid: vector(1, rng) for cid in ids})
    await add_claim(db, a, verifiability=0.9, kind="statistic")  # positive
    claim_b = await add_claim(db, b, verifiability=0.9, kind="statistic")  # reviewer overrides
    await add_extract_job(db, c)  # no claim: negative
    await add_claim(db, d, verifiability=0.9, kind="opinion")  # not positive, has a claim
    # e: nothing; f: weak claim; g: extraction job still failed
    await add_claim(db, f, verifiability=0.3, kind="statistic")
    await add_extract_job(db, g, status="failed")
    async with db.transaction() as s:
        group_id = (
            await s.execute(
                text(
                    "INSERT INTO claim_groups (representative_claim_id, level, state)"
                    " VALUES (:c, 'N3', 'verified') RETURNING id"
                ),
                {"c": claim_b},
            )
        ).scalar_one()
        await s.execute(
            text("INSERT INTO claim_group_members (group_id, claim_id) VALUES (:g, :c)"),
            {"g": group_id, "c": claim_b},
        )
        verdict_id = (
            await s.execute(
                text(
                    "INSERT INTO verdicts (claim_group_id, veracity, context_need, status)"
                    " VALUES (:g, 'false', 'none', 'active') RETURNING id"
                ),
                {"g": group_id},
            )
        ).scalar_one()
        await s.execute(
            text(
                "INSERT INTO reviews (verdict_id, reviewer, decision, outcome, labels)"
                " VALUES (:v, 'staff:1', 'reject', 'rejected', ARRAY['not_worth_checking'])"
            ),
            {"v": verdict_id},
        )
    async with db.session() as s:
        samples = await load_samples(s, since=NOW - timedelta(days=1), model=MODEL)
    assert {x.content_id: x.label for x in samples} == {a: 1, b: 0, c: 0}
    assert all(x.lang == "fr" and x.level == "N1" and len(x.vector) == DIMS for x in samples)
    async with db.session() as s:
        assert await load_samples(s, since=NOW - timedelta(days=1), model="other") == []


# -------------------------------------------------------------------- end to end


async def test_train_then_activate_end_to_end(db: Database) -> None:
    await build_dataset(db)
    settings = make_settings(llm_fake=True)

    assert await run_train(settings, dry_run=True) == 0
    assert await scalar(db, "SELECT count(*) FROM router_models") == 0

    assert await run_train(settings, min_samples=200, seed=1) == 0
    row = (
        await _fetch(
            db,
            "SELECT version, active, embedding_model, dims, sample_size, cardinality(weights),"
            " metrics FROM router_models",
        )
    )[0]
    version, active, model, dims, size, n_weights, metrics = row
    assert (version, active, model, dims, size, n_weights) == (1, False, MODEL, 512, 240, 512)
    assert metrics["recall"] >= 0.9 and metrics["auc"] >= 0.95
    assert metrics["train_size"] + metrics["holdout_size"] == 240
    assert metrics["strata"] == {"fr|N1|0": 120, "fr|N1|1": 120}
    distillation = metrics["distillation"]
    assert distillation["holdout_size"] == metrics["holdout_size"]
    assert distillation["recall_loss"] <= 0.02 and distillation["e2_call_reduction"] >= 0.5
    assert distillation["meets_criterion"] is True

    assert await run_activate(settings, version=1, force=False, reason="first model") == 0
    assert await scalar(db, "SELECT active FROM router_models WHERE version = 1") is True

    # a second, weaker model is refused below the recall target, accepted with --force
    async with db.transaction() as s:
        weak = await create_router_model(
            s,
            SYSTEM,
            embedding_model=MODEL,
            weights=[0.0] * 512,
            bias=0.0,
            metrics={"recall": 0.5},
            sample_size=10,
        )
    assert weak.version == 2
    assert await run_activate(settings, version=2, force=False, reason="weak") == 1
    assert await scalar(db, "SELECT active FROM router_models WHERE version = 1") is True
    assert await run_activate(settings, version=2, force=True, reason="weak but forced") == 0
    assert await scalar(db, "SELECT version FROM router_models WHERE active") == 2
    assert await scalar(db, "SELECT count(*) FROM router_models WHERE active") == 1
    assert await run_activate(settings, version=99, force=True, reason="missing") == 1


async def _fetch(db: Database, sql: str) -> list[Any]:
    async with db.session() as s:
        return list((await s.execute(text(sql))).all())


async def test_not_enough_samples_writes_nothing(
    db: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    await build_dataset(db, per_class=20)
    code = await run_train(make_settings(llm_fake=True), min_samples=200)
    assert code == EXIT_NOT_ENOUGH_SAMPLES == 2
    assert "Not enough labelled samples" in capsys.readouterr().out
    assert await scalar(db, "SELECT count(*) FROM router_models") == 0


async def test_activation_refuses_a_model_of_another_embedding(db: Database) -> None:
    async with db.transaction() as s:
        await create_router_model(
            s,
            SYSTEM,
            embedding_model="some-other-model",
            weights=[0.0] * 512,
            bias=0.0,
            metrics={"recall": 0.99},
            sample_size=10,
        )
    assert await run_activate(make_settings(llm_fake=True), version=1, force=True, reason="x") == 1
    assert await scalar(db, "SELECT count(*) FROM router_models WHERE active") == 0


def test_cli_arguments() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "factcheck",
            "train-router",
            "--days",
            "7",
            "--min-samples",
            "50",
            "--holdout",
            "0.3",
            "--per-stratum-cap",
            "10",
            "--seed",
            "3",
            "--dry-run",
        ]
    )
    assert (args.action, args.days, args.min_samples, args.holdout) == ("train-router", 7, 50, 0.3)
    assert (args.per_stratum_cap, args.seed, args.dry_run) == (10, 3, True)
    args = parser.parse_args(
        ["factcheck", "activate-router", "--version", "2", "--force", "--reason", "why"]
    )
    assert (args.action, args.version, args.force, args.reason) == (
        "activate-router",
        2,
        True,
        "why",
    )
    defaults = parser.parse_args(["factcheck", "train-router"])
    assert (defaults.days, defaults.min_samples, defaults.holdout) == (14, 200, 0.2)
    with pytest.raises(SystemExit):
        parser.parse_args(["factcheck", "activate-router", "--version", "2"])
