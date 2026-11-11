"""``tower factcheck eval``, E3 layer: new JSONL fields, pair metrics, bands, E7 metric,
end-to-end run on the example file (fake provider, real PostgreSQL for the caches)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tower.core.adapters.fake_llm import FakeEmbedder, FakeLLMProvider
from tower.core.db.database import Database
from tower.core.domain.errors import InvalidInput
from tower.core.domain.factcheck.values import Band, BandSide, Veracity
from tower.core.factcheck.profiles import default_profile
from tower.core.llm.admin import run_seed
from tower.core.llm.embeddings import EmbeddingService
from tower.core.llm.prompts import PromptRegistry
from tower.core.llm.router import LLMRouter
from tower.services.investigate import evaluation as ev
from tower.services.investigate import evaluation_e3 as e3

from .conftest import make_settings
from .test_factcheck_eval import case_line, make_case, make_outcome
from .test_investigate_pipeline import FakeAtlas, default_docs

EXAMPLES = Path(__file__).parent / "fixtures" / "factcheck_eval_examples.jsonl"
REFERENCE = {"value": 7.4, "unit": "percent", "previous_value": 7.3, "territory": "FR"}


# ------------------------------------------------------------------------- parsing


def test_parse_new_fields() -> None:
    line = case_line(
        id="a",
        group="g1",
        pair_id="p1",
        pair_expect="different",
        category="interlingual_pair",
        reference={**REFERENCE, "period_start": "2025-10-01", "period_end": "2025-12-31"},
        expected_band={"band": "within_1_tau", "side": "above"},
        expected_outdated=True,
    )
    case = ev.parse_cases(line)[0]
    assert (case.group, case.pair_id, case.pair_expect) == ("g1", "p1", "different")
    assert case.category == "interlingual_pair"
    assert case.reference is not None
    assert case.reference.value == Decimal("7.4") and case.reference.previous_value == Decimal(
        "7.3"
    )
    assert case.reference.period_end == date(2025, 12, 31)
    assert case.expected_band == (Band.WITHIN_1_TAU, BandSide.ABOVE)
    assert case.expected_outdated is True


def test_old_files_stay_valid() -> None:
    case = ev.parse_cases(case_line())[0]
    assert case.group is None and case.reference is None and case.expected_band is None


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"pair_id": "p", "pair_expect": "maybe"}, "pair_expect"),
        ({"pair_expect": "same"}, "pair_expect"),
        ({"group": " "}, "group"),
        ({"reference": "7.4"}, "reference"),
        ({"reference": {"unit": "percent"}}, "reference.value"),
        ({"reference": {"value": "abc"}}, "reference.value"),
        ({"reference": {"value": 1, "previous_value": [1]}}, "reference.previous_value"),
        ({"reference": {"value": 1, "period_start": "2025/10/01"}}, "reference.period_start"),
        ({"reference": {"value": 1, "unit": ""}}, "reference.unit"),
        (
            {"reference": REFERENCE, "expected_band": {"band": "far", "side": "none"}},
            "expected_band.band",
        ),
        (
            {"reference": REFERENCE, "expected_band": {"band": "contains", "side": "left"}},
            "expected_band.side",
        ),
        ({"reference": REFERENCE, "expected_outdated": "yes"}, "expected_outdated"),
        ({"expected_band": {"band": "contains", "side": "none"}}, "reference"),
        ({"expected_outdated": False}, "reference"),
    ],
)
def test_parse_bad_new_fields(overrides: dict[str, Any], field: str) -> None:
    text = case_line() + "\n" + case_line(id="c2", **overrides)
    with pytest.raises(InvalidInput, match=rf"Line 2, field '{field}'"):
        ev.parse_cases(text)


def test_example_file_has_the_e3_cases() -> None:
    cases = ev.load_cases(EXAMPLES)
    assert {c.category for c in cases} >= {"trap_pair", "interlingual_pair"}
    assert sum(1 for c in cases if c.reference is not None) >= 2
    assert any(c.expected_outdated for c in cases)
    pairs = e3.expected_pairs(cases)
    assert [(p.expect) for p in pairs] == ["different", "different", "same"]


# ------------------------------------------------------------------ pair planning


def test_expected_pairs_only_listed_pairs_and_dedup() -> None:
    cases = ev.parse_cases(
        "\n".join(
            [
                case_line(id="a", pair_id="p", pair_expect="different"),
                case_line(id="b", pair_id="p", pair_expect="different", group="x"),
                case_line(id="c", group="x"),
                case_line(id="d", group="y"),
                case_line(id="e"),  # no label, no pair: never compared
                case_line(id="m1", pair_id="mirror", category="mirror_pair"),
                case_line(id="m2", pair_id="mirror", category="mirror_pair"),
            ]
        )
    )
    pairs = e3.expected_pairs(cases)
    assert [(p.a, p.b, p.expect) for p in pairs] == [("a", "b", "different"), ("b", "c", "same")]
    # mirror pairs have no expectation: E8 pairs, not E3 ones
    assert e3.needs_e3(cases)
    assert not e3.needs_e3(cases[4:])


# ------------------------------------------------------------------------- metrics


def decision(
    a: str, b: str, expect: str | None, joined: bool, *, category: str = "other", calls: int = 0
) -> e3.PairDecision:
    return e3.PairDecision(
        e3.PairSpec(a, b, expect), joined, "rule", 0.9, calls, category=category, langs="fr-fr"
    )


def test_false_reuse_wrong_merges_missed_groupings_and_purity() -> None:
    report = e3.E3Report(
        decisions=[
            decision("a", "b", "different", True),  # false reuse + wrong merge
            decision("c", "d", "different", False),
            decision("e", "f", "same", False, calls=1),  # missed grouping
            decision("g", "h", "same", True, calls=1),
        ],
        labels={"a": "A", "b": "B", "c": "C", "d": "D", "e": "E", "f": "E", "g": "G", "h": "G"},
    )
    m = e3.compute_e3_metrics(report)
    assert (m.expected_different, m.false_reuse, m.false_reuse_rate) == (2, 1, 0.5)
    assert (m.different_label_pairs, m.wrong_merges, m.wrong_merge_rate) == (2, 1, 0.5)
    assert (m.same_label_pairs, m.missed_groupings, m.missed_grouping_rate) == (2, 1, 0.5)
    # groups: {a,b} (A,B) -> 1/2, {c},{d},{e},{f} -> 1 each, {g,h} -> 2/2 : 8 cases, 7 majority
    assert m.group_purity == pytest.approx(7 / 8)
    assert (m.arbiter_calls, m.arbiter_share) == (2, 0.5)


def test_transitive_groups_and_unlabelled_cases_ignored() -> None:
    report = e3.E3Report(
        decisions=[decision("a", "b", "same", True), decision("b", "c", "same", True)],
        labels={"a": "X", "b": "X", "c": None},
    )
    groups = e3.predicted_groups(report)
    assert len({groups["a"], groups["b"], groups["c"]}) == 1
    assert e3.compute_e3_metrics(report).group_purity == 1.0


def test_interlingual_rate_and_empty_metrics() -> None:
    report = e3.E3Report(
        decisions=[
            decision("a", "b", "same", True, category="interlingual_pair"),
            decision("c", "d", "same", False, category="interlingual_pair"),
        ]
    )
    m = e3.compute_e3_metrics(report)
    assert (m.interlingual_pairs, m.interlingual_joined, m.interlingual_rate) == (2, 1, 0.5)
    empty = e3.compute_e3_metrics(e3.E3Report())
    assert empty.false_reuse_rate is None and empty.group_purity is None
    assert empty.band_accuracy is None and empty.interlingual_rate is None


def test_band_and_outdated_accuracy() -> None:
    within = (Band.WITHIN_1_TAU, BandSide.ABOVE)
    report = e3.E3Report(
        bands=[
            e3.BandDecision("a", Band.WITHIN_1_TAU, BandSide.ABOVE, False, within, False),
            e3.BandDecision("b", Band.CONTAINS, BandSide.NONE, False, within, True),
            e3.BandDecision("c", Band.BEYOND, BandSide.BELOW, True, None, None),  # not scored
        ]
    )
    m = e3.compute_e3_metrics(report)
    assert (m.band_cases, m.band_accuracy) == (2, 0.5)
    assert (m.outdated_cases, m.outdated_accuracy) == (2, 0.5)


def test_false_reuse_mark() -> None:
    assert "✅" in e3.false_reuse_mark(0.0) and "❌" in e3.false_reuse_mark(0.05)
    assert e3.false_reuse_mark(None) == "n/a"


def test_band_decision_uses_settings_tolerances() -> None:
    from tower.core.domain.factcheck.signatures import normalize_signature

    def sig(value: float) -> Any:
        return normalize_signature(
            claim_type="statistic",
            subject=None,
            subject_qid=None,
            predicate=None,
            value_low=value,
            value_high=value,
            unit=None,
            unit_scale=None,
            currency=None,
            value_date=None,
            period_start=None,
            period_end=None,
            place=None,
            primary_jurisdiction="FR",
            polarity="affirmative",
            definition_variant=None,
            comparison_base=None,
            post_date=date(2026, 1, 1),
        )

    settings = make_settings()
    case = make_case("b", reference=REFERENCE, expected_band={"band": "contains", "side": "none"})
    assert case.reference is not None
    # tau = max(0.2, 5 % of 7.4) = 0.37
    same = e3.band_decision(case, sig(7.4), settings)
    assert (same.band, same.side, same.outdated) == (Band.CONTAINS, BandSide.NONE, False)
    above = e3.band_decision(case, sig(7.7), settings)
    assert (above.band, above.side) == (Band.WITHIN_1_TAU, BandSide.ABOVE)
    far = e3.band_decision(case, sig(9.0), settings)
    assert (far.band, far.side) == (Band.BEYOND, BandSide.ABOVE)
    old = e3.band_decision(case, sig(7.3), settings)
    assert old.outdated and old.side is BandSide.BELOW


# ----------------------------------------------------------------------- E7 metric


def test_paid_search_metric() -> None:
    def res(cid: str, searches: int | None) -> ev.CaseResult:
        return ev.CaseResult(make_case(cid), make_outcome(Veracity.TRUE), paid_searches=searches)

    results = [res("a", 0), res("b", 2), res("c", 0), res("d", None)]
    m = ev.compute_metrics(results, high=0.8)
    assert (m.no_paid_search, m.paid_search_measured) == (2, 3)
    assert m.no_paid_search_rate == pytest.approx(2 / 3)
    unmeasured = ev.compute_metrics([res("a", None)], high=0.8)
    assert unmeasured.no_paid_search_rate is None
    run = ev.EvalRun(
        results=results,
        level="N3",
        set_path="s",
        started_at=datetime(2026, 11, 11, tzinfo=UTC),
        fake_provider=True,
    )
    assert "Settled without paid web search" in ev.render_report(run, high=0.8)
    assert "settled without paid web search" in ev.render_summary(run, high=0.8)


# ----------------------------------------------------------------------- rendering


def test_report_renders_e3_section_first_metric() -> None:
    report = e3.E3Report(
        decisions=[decision("a", "b", "different", False, category="trap_pair")],
        bands=[e3.BandDecision("a", Band.CONTAINS, BandSide.NONE, False, None, None)],
        errors={"x": "no_claim_extracted"},
        cost_usd_micros=1500,
    )
    run = ev.EvalRun(
        results=[ev.CaseResult(make_case("a"), make_outcome(Veracity.TRUE))],
        level="N3",
        set_path="s",
        started_at=datetime(2026, 11, 11, tzinfo=UTC),
        fake_provider=True,
        e3=report,
    )
    text = ev.render_report(run, high=0.8)
    lines = e3.render_e3_section(report)
    assert lines[0] == "## E3: matching, groups and bands"
    assert lines[2].startswith("**False reuse rate: 0.0% (target < 1%) ✅**")
    assert "### E3 by category" in text and "### Pairs" in text and "### Bands" in text
    assert "no_claim_extracted" in text and "$0.0015" in ev.render_report(run, high=0.8)
    assert "E3: false reuse" in ev.render_summary(run, high=0.8)


def test_e3_only_report_skips_the_pipeline_sections() -> None:
    run = ev.EvalRun(
        results=[ev.CaseResult(make_case("a"))],
        level="N3",
        set_path="s",
        started_at=datetime(2026, 11, 11, tzinfo=UTC),
        fake_provider=True,
        e3=e3.E3Report(),
        e3_only=True,
    )
    text = ev.render_report(run, high=0.8)
    assert "E3 only" in text and "## E3:" in text and "## By category" not in text
    summary = ev.render_summary(run, high=0.8)
    assert "E3 only" in summary and len(summary.splitlines()) <= 4


# --------------------------------------------------------------------- end to end


def build_router(db: Database) -> LLMRouter:
    providers = {n: FakeLLMProvider(n) for n in ("openai", "openai_shared", "anthropic")}
    return LLMRouter(db, providers, PromptRegistry.default(), usage_tier=1, free_margin=0.1)


async def test_e3_end_to_end_is_deterministic_and_replays_for_free(db: Database) -> None:
    settings = make_settings(llm_fake=True)
    await run_seed(settings)
    router = build_router(db)
    embedder = EmbeddingService(db, FakeEmbedder())
    cases = ev.load_cases(EXAMPLES)

    async def run(**kw: Any) -> ev.EvalRun:
        return await ev.run_eval(
            router,
            FakeAtlas([default_docs()]),
            cases,
            settings=settings,
            today=date(2026, 3, 1),
            embedder=embedder,
            **kw,
        )

    first = await run(e3_only=True)
    assert first.e3 is not None and not first.e3.errors
    assert all(r.outcome is None and r.error is None for r in first.results)
    m = e3.compute_e3_metrics(first.e3)
    assert m.false_reuse == 0 and m.wrong_merges == 0 and m.missed_groupings == 0
    assert m.interlingual_pairs == 1 and m.interlingual_rate == 1.0
    assert m.band_accuracy == 1.0 and m.outdated_accuracy == 1.0
    assert m.group_purity == 1.0 and m.arbiter_calls >= 1
    decisions = {(d.spec.a, d.spec.b): d for d in first.e3.decisions}
    negation = decisions["ex-trap-vaccine-affirm", "ex-trap-vaccine-negation"]
    assert not negation.joined
    value = decisions["ex-trap-unemployment-7", "ex-trap-unemployment-9"]
    assert not value.joined and value.rule == "new:signature:value"
    intl = decisions["ex-intl-en", "ex-intl-de"]
    assert intl.joined and intl.rule == "identical_signature:arbiter"

    second = await run(e3_only=True)
    assert second.e3 is not None
    assert second.total_cost_usd_micros == 0 and second.e3.cost_usd_micros == 0
    assert [(d.spec, d.joined, d.rule) for d in second.e3.decisions] == [
        (d.spec, d.joined, d.rule) for d in first.e3.decisions
    ]
    assert "False reuse rate: 0.0%" in ev.render_report(second, high=settings.fc_confidence_high)


async def test_e3_pair_with_a_failed_case_is_an_error_row(db: Database) -> None:
    settings = make_settings(llm_fake=True)
    await run_seed(settings)
    lines = [
        case_line(id="a", claim="Short.", lang="fr", pair_id="p", pair_expect="different"),
        case_line(
            id="b",
            claim="Le taux de chômage est de 7 % en France.",
            lang="fr",
            pair_id="p",
            pair_expect="different",
        ),
    ]
    report = await e3.run_e3(
        build_router(db),
        EmbeddingService(db, FakeEmbedder()),
        ev.parse_cases("\n".join(lines)),
        settings=settings,
        profile=default_profile(settings),
        today=date(2026, 3, 1),
    )
    assert report.errors["a"] == "no_claim_extracted"
    assert report.errors["a|b"] == "case_failed" and not report.decisions
    assert e3.compute_e3_metrics(report).pair_errors == 1


def test_json_line_of_the_example_reference_is_plain_json() -> None:
    for line in EXAMPLES.read_text(encoding="utf-8").splitlines():
        json.loads(line)
