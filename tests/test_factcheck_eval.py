"""``tower factcheck eval``: JSONL parsing, metrics, report, end-to-end run, CLI parsing."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from tower.cli import build_parser
from tower.core.adapters.fake_llm import FakeLLMProvider
from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain.errors import InvalidInput
from tower.core.domain.factcheck.confidence import ConfidenceBand
from tower.core.domain.factcheck.values import Agreement, ContextNeed, Veracity
from tower.core.llm.admin import run_seed
from tower.core.llm.prompts import PromptRegistry
from tower.core.llm.router import LLMRouter
from tower.services.investigate import evaluation as ev
from tower.services.investigate import pipeline as pl

from .conftest import make_settings
from .test_investigate_pipeline import FakeAtlas, default_docs

EXAMPLES = Path(__file__).parent / "fixtures" / "factcheck_eval_examples.jsonl"


def case_line(**overrides: Any) -> str:
    base: dict[str, Any] = {
        "id": "c1",
        "claim": "Unemployment fell to 7 percent.",
        "lang": "en",
        "jurisdiction": "FR",
        "date": "2026-01-02",
        "expected_veracity": "true",
        "expected_context_need": "none",
    }
    base.update(overrides)
    return json.dumps({k: v for k, v in base.items() if v is not None})


# ------------------------------------------------------------------------- parsing


def test_parse_valid_set_and_defaults() -> None:
    cases = ev.parse_cases(case_line() + "\n\n   \n" + case_line(id="c2", claim_type="statistic"))
    assert [c.id for c in cases] == ["c1", "c2"]
    assert cases[0].claim_type == "other" and cases[0].category == "other"
    assert cases[0].expected_veracity is Veracity.TRUE and cases[1].claim_type == "statistic"
    assert cases[0].date == date(2026, 1, 2) and not cases[0].example


def test_parse_limit_keeps_first_cases() -> None:
    text = "\n".join(case_line(id=f"c{i}") for i in range(5))
    assert [c.id for c in ev.parse_cases(text, limit=2)] == ["c0", "c1"]


def test_parse_missing_field_names_line_and_field() -> None:
    text = case_line() + "\n" + case_line(id="c2", expected_veracity=None)
    with pytest.raises(InvalidInput, match=r"Line 2, field 'expected_veracity'"):
        ev.parse_cases(text)


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"expected_veracity": "maybe"}, "expected_veracity"),
        ({"expected_context_need": "lots"}, "expected_context_need"),
        ({"claim_type": "rumour"}, "claim_type"),
        ({"expected_context_types": ["nope"]}, "expected_context_types"),
        ({"category": "weird"}, "category"),
        ({"jurisdiction": "France"}, "jurisdiction"),
        ({"date": "02/01/2026"}, "date"),
        ({"claim": "x" * 301}, "claim"),
    ],
)
def test_parse_bad_values(overrides: dict[str, Any], field: str) -> None:
    with pytest.raises(InvalidInput, match=f"Line 1, field '{field}'"):
        ev.parse_cases(case_line(**overrides))


def test_parse_bad_json_and_duplicate_id() -> None:
    with pytest.raises(InvalidInput, match="Line 1"):
        ev.parse_cases("{not json")
    with pytest.raises(InvalidInput, match="duplicate id"):
        ev.parse_cases(case_line() + "\n" + case_line())


def test_example_file_loads() -> None:
    cases = ev.load_cases(EXAMPLES)
    assert len(cases) == 5 and all(c.example and c.notes for c in cases)
    assert {c.category for c in cases} >= {"established_fact_denial", "unverifiable"}
    assert {c.lang for c in cases} == {"fr", "en"}


# ------------------------------------------------------------------------- metrics


def make_outcome(
    veracity: Veracity | None,
    *,
    confidence: float | None = 0.5,
    need: ContextNeed = ContextNeed.NONE,
    types: list[str] | None = None,
    cit: tuple[int, int] = (0, 0),
    escalated: bool = False,
    cost: int = 0,
    latency: int = 100,
) -> pl.InMemoryOutcome:
    return pl.InMemoryOutcome(
        veracity=veracity,
        context_need=need,
        context_types=types or [],
        confidence=confidence,
        band=ConfidenceBand.LOW,
        agreement=Agreement.IDENTICAL,
        escalated=escalated,
        depth="standard",
        rounds=1,
        refused=[],
        citations_total=cit[1],
        citations_valid=cit[0],
        cost_usd_micros=cost,
        latency_ms=latency,
        notes=[],
        flags={},
        unverifiable_at_framing=False,
    )


def make_case(cid: str = "c", **overrides: Any) -> ev.EvalCase:
    return ev.parse_cases(case_line(id=cid, **overrides))[0]


def result(
    expected: str, predicted: Veracity | None, *, cid: str = "c", **kw: Any
) -> ev.CaseResult:
    case_kw = {
        k: kw.pop(k) for k in ("category", "pair_id", "expected_context_need", "lang") if k in kw
    }
    case = make_case(cid, expected_veracity=expected, **case_kw)
    return ev.CaseResult(case, make_outcome(predicted, **kw))


def test_accuracy_exact_and_by_side() -> None:
    rs = [
        result("true", Veracity.TRUE, cid="a"),
        result("true", Veracity.MOSTLY_TRUE, cid="b"),
        result("false", Veracity.MISLEADING, cid="c"),
        result("unverifiable", Veracity.UNVERIFIABLE, cid="d"),
    ]
    m = ev.compute_metrics(rs, high=0.8)
    assert m.exact_accuracy == 0.5 and m.side_accuracy == 1.0
    assert m.accuracy_by_side == {"supports": 0.5, "refutes": 0.0, "abstains": 1.0}


def test_false_confident_uses_threshold_and_only_true_false() -> None:
    rs = [
        result("false", Veracity.TRUE, cid="a", confidence=0.85),  # false confident
        result("false", Veracity.TRUE, cid="b", confidence=0.79),  # below threshold
        result("true", Veracity.MISLEADING, cid="c", confidence=0.95),  # not true/false
        result("true", Veracity.TRUE, cid="d", confidence=0.95),  # correct
    ]
    m = ev.compute_metrics(rs, high=0.8)
    assert m.false_confident == 1 and m.false_confident_rate == 0.25
    assert ev.compute_metrics(rs, high=0.85).false_confident == 1
    assert ev.compute_metrics(rs, high=0.9).false_confident == 0
    assert ev.compute_metrics(rs, high=0.99).false_confident == 0


def test_abstention_rules() -> None:
    rs = [
        result("false", Veracity.UNVERIFIABLE, cid="a", category="established_fact_denial"),
        result("false", Veracity.FALSE, cid="b", category="established_fact_denial"),
        result("unverifiable", Veracity.TRUE, cid="c", category="debated"),
        result("unverifiable", Veracity.MISLEADING, cid="d", category="debated"),
        result("unverifiable", Veracity.UNVERIFIABLE, cid="e", category="debated"),
    ]
    m = ev.compute_metrics(rs, high=0.8)
    assert m.abstention_errors == 2  # a (abstained on a denial) and c (decisive on debated)
    assert m.abstention_rate == pytest.approx(2 / 5)


def test_unverifiable_at_framing_counts_as_abstention() -> None:
    case = make_case("p", expected_veracity="unverifiable", claim_type="prediction")
    outcome = make_outcome(None, confidence=None)
    outcome.context_need = None
    outcome.unverifiable_at_framing = True
    m = ev.compute_metrics([ev.CaseResult(case, outcome)], high=0.8)
    assert m.scored == 1 and m.exact_accuracy == 1.0 and m.abstention_rate == 1.0
    assert m.context_need_accuracy == 1.0


def test_context_need_type_recall_citations_escalation() -> None:
    rs = [
        result(
            "true",
            Veracity.TRUE,
            cid="a",
            expected_context_need="useful",
            need=ContextNeed.USEFUL,
            types=["scope"],
            cit=(2, 3),
            escalated=True,
        ),
        result("true", Veracity.TRUE, cid="b", cit=(1, 1)),
    ]
    rs[0].case = make_case(
        "a", expected_context_types=["scope", "outdated"], expected_context_need="useful"
    )
    m = ev.compute_metrics(rs, high=0.8)
    assert m.context_need_accuracy == 1.0
    assert m.context_type_recall == 0.5
    assert m.citation_validity == pytest.approx(3 / 4) and m.citations_total == 4
    assert m.escalation_rate == 0.5


def test_pair_consistency() -> None:
    rs = [
        result("true", Veracity.TRUE, cid="a", pair_id="p1"),
        result("true", Veracity.TRUE, cid="b", pair_id="p1"),
        result("true", Veracity.TRUE, cid="c", pair_id="p2"),
        result("true", Veracity.FALSE, cid="d", pair_id="p2"),
        result("true", Veracity.TRUE, cid="e", pair_id="lonely"),
    ]
    m = ev.compute_metrics(rs, high=0.8)
    assert m.pairs == 2 and m.pair_consistency == 0.5


def test_percentile_cost_latency_and_errors() -> None:
    assert ev.percentile95([]) is None
    assert ev.percentile95([5]) == 5
    assert ev.percentile95(list(range(1, 101))) == 95
    rs = [
        result("true", Veracity.TRUE, cid=f"c{i}", cost=10 * i, latency=100 * i)
        for i in range(1, 5)
    ]
    rs.append(ev.CaseResult(make_case("bad"), error="TWR-LLM-001"))
    m = ev.compute_metrics(rs, high=0.8)
    assert (m.cases, m.scored, m.errors) == (5, 4, 1)
    assert m.mean_cost_usd_micros == 25 and m.mean_latency_ms == 250 and m.p95_latency_ms == 400


def test_empty_metrics_are_none() -> None:
    m = ev.compute_metrics([], high=0.8)
    assert m.exact_accuracy is None and m.false_confident_rate is None


# ------------------------------------------------------------------------- report


def test_report_rendering_flags_examples() -> None:
    example = make_case("ex1", example=True, category="debated")
    rs = [
        ev.CaseResult(example, make_outcome(Veracity.TRUE, confidence=0.9, cit=(1, 2))),
        result("false", Veracity.FALSE, cid="real", lang="fr"),
        ev.CaseResult(make_case("boom"), error="TWR-FCK-004"),
    ]
    run = ev.EvalRun(
        results=rs,
        level="N3",
        set_path="set.jsonl",
        started_at=datetime(2026, 11, 11, tzinfo=UTC),
        fake_provider=True,
        prompt_versions={"judge": "abc123"},
    )
    report = ev.render_report(run, high=0.8)
    for heading in ("# Fact-check evaluation report", "## Metrics", "## By category"):
        assert heading in report
    assert "## By language" in report and "## Cases" in report
    assert "Example cases: 1 - not reference data" in report
    assert "Fake provider: on" in report and "judge abc123" in report
    assert "ex1 (example)" in report and "TWR-FCK-004" in report and "1/2" in report
    assert "Set: set.jsonl" in report
    summary = ev.render_summary(run, high=0.8)
    assert "3 cases" in summary and len(summary.splitlines()) <= 6


# ------------------------------------------------------------------------- end to end


async def test_end_to_end_examples_with_fake_provider_and_cache(db: Database) -> None:
    settings = make_settings(llm_fake=True)
    await run_seed(settings)
    providers = {name: FakeLLMProvider(name) for name in ("openai", "openai_shared", "anthropic")}
    router = LLMRouter(db, providers, PromptRegistry.default(), usage_tier=1, free_margin=0.1)
    cases = ev.load_cases(EXAMPLES)
    first = await ev.run_eval(
        router,
        FakeAtlas([default_docs()]),
        cases,
        settings=settings,
        concurrency=2,
        set_path=str(EXAMPLES),
        today=date(2026, 3, 1),
    )
    assert len(first.results) == 5
    assert all(r.error is None and r.scored for r in first.results), [
        r.error for r in first.results
    ]
    assert first.total_cost_usd_micros >= 0
    report = ev.render_report(first, high=settings.fc_confidence_high)
    assert "Example cases: 5" in report
    second = await ev.run_eval(
        router,
        FakeAtlas([default_docs()]),
        cases,
        settings=settings,
        concurrency=2,
        set_path=str(EXAMPLES),
        today=date(2026, 3, 1),
    )
    assert second.total_cost_usd_micros == 0
    assert [r.predicted for r in second.results] == [r.predicted for r in first.results]


async def test_a_failing_case_is_an_error_row_and_the_run_continues() -> None:
    class Boom:
        async def collect(self, request: Any) -> Any:
            raise RuntimeError("atlas down")

    class NoLLM:
        async def complete(self, request: Any) -> Any:
            raise RuntimeError("no model")

    cases = ev.parse_cases(case_line(id="a") + "\n" + case_line(id="b"))
    run = await ev.run_eval(NoLLM(), Boom(), cases, settings=make_settings())
    assert [r.error for r in run.results] == ["RuntimeError", "RuntimeError"]
    assert ev.compute_metrics(run.results, high=0.8).errors == 2
    assert "RuntimeError" in ev.render_report(run, high=0.8)


# ------------------------------------------------------------------------- CLI


def test_cli_eval_arguments() -> None:
    args = build_parser().parse_args(
        [
            "factcheck",
            "eval",
            "--set",
            "x.jsonl",
            "--limit",
            "3",
            "--report",
            "r.md",
            "--level",
            "N4",
            "--concurrency",
            "4",
        ]
    )
    assert (args.set, args.limit, args.report, args.level, args.concurrency) == (
        "x.jsonl",
        3,
        "r.md",
        "N4",
        4,
    )
    defaults = build_parser().parse_args(["factcheck", "eval", "--set", "x.jsonl"])
    assert (defaults.level, defaults.concurrency, defaults.limit) == ("N3", 2, None)
    with pytest.raises(SystemExit):
        build_parser().parse_args(["factcheck", "eval"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["factcheck", "eval", "--set", "x", "--level", "N1"])


def test_cli_seed_registry_argument() -> None:
    args = build_parser().parse_args(["factcheck", "seed-registry"])
    assert args.action == "seed-registry"


async def test_eval_command_exits_2_without_routes(
    db: Database, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await ev.run_command(
        settings, set_path=str(EXAMPLES), limit=None, report=None, level="N3", concurrency=1
    )
    assert code == 2 and "tower llm seed" in capsys.readouterr().out
