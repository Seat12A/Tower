"""``tower factcheck eval --batch``: contamination, omission, per-position metrics (synthetic
outputs and a scripted model), CLI flags, deterministic run with the fake provider, and the
load test script's ``--help``."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from tower import cli
from tower.core.adapters.fake_llm import FakeEmbedder
from tower.core.db.database import Database
from tower.core.factcheck.schemas import ExtractBatchOutput, ExtractOutput
from tower.core.llm.admin import run_seed
from tower.core.llm.embeddings import EmbeddingService
from tower.services.investigate import evaluation as ev
from tower.services.investigate import evaluation_batch as eb

from .conftest import make_settings
from .test_factcheck_eval import make_case
from .test_factcheck_eval_e3 import EXAMPLES, build_router

ROOT = Path(__file__).resolve().parent.parent


def post(
    case_id: str, batch: int, position: int, text: str, batch_claims: list[str], solo: list[str]
) -> eb.PostRun:
    return eb.PostRun(case_id, batch, position, text, tuple(batch_claims), tuple(solo))


def report(posts: list[eb.PostRun], *, token: str | None = None) -> eb.BatchEvalReport:
    rep = eb.BatchEvalReport(set_path="x", batch_size=10)
    for index in sorted({p.batch for p in posts}):
        members = tuple(p.case_id for p in posts if p.batch == index)
        rep.batches.append(eb.BatchRun(index, members, token, False, len(members) + 1, 3000))
    rep.posts = posts
    rep.solo_cost_usd_micros = 2000 * len(posts)
    return rep


# ---------------------------------------------------------------------------- metrics


def test_clean_batch_has_no_contamination_and_no_omission() -> None:
    rep = report(
        [
            post("a", 0, 0, "Il y a 120 morts.", ["120 morts"], ["120 morts"]),
            post("b", 0, 1, "Le prix est de 45 euros.", ["45 euros"], ["45 euros"]),
        ]
    )
    m = eb.compute_batch_metrics(rep)
    assert m.contamination_rate == 0 and m.omission_rate == 0
    assert m.claims_delta == 0 and m.omission_by_position == {0: 0.0, 1: 0.0}
    assert m.batch_cost_per_post_usd_micros == 1500 and m.solo_cost_per_post_usd_micros == 2000


def test_figure_copied_from_another_post_is_contamination() -> None:
    rep = report(
        [
            post("a", 0, 0, "Il y a 120 morts.", ["120 morts"], ["120 morts"]),
            post("b", 0, 1, "Le prix est de 45 euros.", ["45 euros et 120 morts"], ["45 euros"]),
        ]
    )
    m = eb.compute_batch_metrics(rep)
    assert m.contaminated_posts == 1 and m.contamination_rate == 0.5


def test_canary_figure_in_a_post_is_contamination() -> None:
    rep = report(
        [post("a", 0, 0, "Il y a 120 morts.", ["120 morts, 654 321 visiteurs"], ["120 morts"])],
        token="654321",
    )
    assert eb.compute_batch_metrics(rep).contamination_rate == 1.0


def test_omission_and_per_position_rates() -> None:
    rep = report(
        [
            post(
                "a",
                0,
                0,
                "120 morts et 7 blessés",
                ["120 morts et 7 blessés"],
                ["120 morts", "7 blessés"],
            ),
            post("b", 0, 1, "120 morts et 7 blessés", ["120 morts"], ["120 morts", "7 blessés"]),
            post("c", 1, 1, "Prix 45 euros", [], ["45 euros"]),
        ]
    )
    m = eb.compute_batch_metrics(rep)
    assert m.omitted_posts == 2 and m.omission_rate == 2 / 3
    assert m.omission_by_position == {0: 0.0, 1: 1.0}
    assert m.claims_delta == -1.0


def test_empty_report_has_no_rates() -> None:
    m = eb.compute_batch_metrics(eb.BatchEvalReport(set_path="x", batch_size=10))
    assert m.contamination_rate is None and m.omission_rate is None and m.omission_by_position == {}
    assert "n/a" in eb.render_batch_summary(eb.BatchEvalReport(set_path="x", batch_size=10))


def test_report_renders_positions() -> None:
    rep = report([post("a", 0, 2, "120 morts", ["120 morts"], ["120 morts"])])
    text = eb.render_batch_report(rep)
    assert "Omission by position" in text and "| 2 | 0.0% |" in text


# ------------------------------------------------------------------ scripted model run


@dataclass
class Result:
    output: Any
    cost_usd_micros: int = 1000


class ScriptedLLM:
    """Solo: one claim carrying the post's figures. Batch: same, but the first post's
    figure leaks into the second post's claims, and the last post is dropped."""

    def __init__(self, *, leak: bool, drop_last: bool = False) -> None:
        self.leak, self.drop_last = leak, drop_last

    async def complete(self, request: Any) -> Result:
        from tower.core.adapters.fake_llm import _extract, _unmark_all

        data = _unmark_all(request.input)
        if request.schema is ExtractOutput:
            return Result(ExtractOutput.model_validate(_extract(data)))
        posts = [_extract({"today": data["today"], "post": p}) for p in data["posts"]]
        if self.leak and len(posts) > 2:
            first = posts[0]["claims"][0]["text_original"]
            posts[1]["claims"][0]["text_original"] += f" ({first})"
        if self.drop_last:
            posts = posts[:-1]
        return Result(ExtractBatchOutput.model_validate({"posts": posts}), 5000)


class _Vectors:
    def __init__(self, vectors: list[list[float]]) -> None:
        self.vectors = vectors
        self.cost_usd_micros = 0


class OrthogonalEmbedder:
    async def embed(self, texts: Any, *, level: str, ref: str) -> _Vectors:
        n = len(texts)
        return _Vectors([[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)])


def distinct_cases(n: int) -> list[ev.EvalCase]:
    return [
        make_case(f"c{i}", claim=f"Le budget est de {100 + i * 7} millions d'euros.", lang="fr")
        for i in range(n)
    ]


async def test_scripted_router_leak_is_measured() -> None:
    cases = distinct_cases(4)
    rep = await eb.run_batch_eval(
        ScriptedLLM(leak=True),
        OrthogonalEmbedder(),
        cases,
        settings=make_settings(llm_fake=True),
        batch_size=10,
        today=date(2026, 3, 1),
    )
    assert len(rep.batches) == 1 and len(rep.posts) == 4
    assert rep.batches[0].size_sent == 5
    m = eb.compute_batch_metrics(rep)
    assert m.contaminated_posts == 1 and m.omitted_posts == 0
    assert {p.position for p in rep.posts} <= set(range(5))


async def test_scripted_router_dropped_post_is_an_omission() -> None:
    cases = distinct_cases(3)
    rep = await eb.run_batch_eval(
        ScriptedLLM(leak=False, drop_last=True),
        OrthogonalEmbedder(),
        cases,
        settings=make_settings(llm_fake=True),
        batch_size=3,
        today=date(2026, 3, 1),
    )
    m = eb.compute_batch_metrics(rep)
    assert m.contamination_rate == 0
    # The dropped entry is either the canary (nothing lost) or one of the real posts.
    assert m.omitted_posts in (0, 1)
    assert sum(not p.returned for p in rep.posts) == m.omitted_posts


async def test_batch_size_one_runs_posts_alone() -> None:
    rep = await eb.run_batch_eval(
        ScriptedLLM(leak=False),
        OrthogonalEmbedder(),
        distinct_cases(3),
        settings=make_settings(llm_fake=True),
        batch_size=1,
        today=date(2026, 3, 1),
    )
    assert len(rep.batches) == 3 and all(len(b.case_ids) == 1 for b in rep.batches)


async def test_fake_provider_run_is_deterministic_without_contamination(db: Database) -> None:
    settings = make_settings(llm_fake=True)
    await run_seed(settings)
    router = build_router(db)
    embedder = EmbeddingService(db, FakeEmbedder())
    cases = ev.load_cases(EXAMPLES)

    async def run() -> eb.BatchEvalReport:
        return await eb.run_batch_eval(
            router, embedder, cases, settings=settings, batch_size=5, today=date(2026, 3, 1)
        )

    first, second = await run(), await run()
    assert not first.errors and first.posts
    m = eb.compute_batch_metrics(first)
    assert m.contamination_rate == 0 and m.canary_batches_contaminated == 0
    assert m.omission_rate == 0
    assert eb.render_batch_summary(first) == eb.render_batch_summary(second)
    assert [p.batch_claims for p in first.posts] == [p.batch_claims for p in second.posts]


# ------------------------------------------------------------------------------ CLI


def test_cli_batch_flags_parse() -> None:
    args = cli.build_parser().parse_args(
        ["factcheck", "eval", "--set", "s.jsonl", "--batch", "--batch-size", "6"]
    )
    assert args.batch is True and args.batch_size == 6 and args.set == "s.jsonl"
    default = cli.build_parser().parse_args(["factcheck", "eval", "--set", "s.jsonl"])
    assert default.batch is False and default.batch_size == 10


def test_loadtest_script_help_works() -> None:
    out = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts" / "loadtest_wave.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert out.returncode == 0
    for flag in ("--posts", "--minutes", "--narrative-variants", "--watched-post-every"):
        assert flag in out.stdout
