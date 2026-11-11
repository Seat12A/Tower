"""Evaluation tool behind ``tower factcheck eval``.

Loads a JSONL set of expected outcomes, runs every case through the production pipeline
(``pipeline.run_in_memory``: same code, same router, same checks, no investigation row),
computes the metrics of docs/factcheck/evaluation.md and renders a Markdown report.

Everything except ``run_eval`` is pure so the metrics are testable on synthetic outcomes.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from tower.core.config import Settings
from tower.core.domain.errors import InvalidInput
from tower.core.domain.factcheck.values import (
    ClaimType,
    ContextNeed,
    ContextType,
    Side,
    Veracity,
    side_of,
)
from tower.core.factcheck.inputs import ClaimView
from tower.services.investigate import pipeline as pl

__all__ = [
    "CATEGORIES",
    "CLAIM_MAX_CHARS",
    "CaseResult",
    "EvalCase",
    "Metrics",
    "compute_metrics",
    "load_cases",
    "parse_cases",
    "percentile95",
    "render_report",
    "render_summary",
    "run_eval",
]

CATEGORIES = frozenset(
    {
        "published_factcheck",
        "true_banal",
        "unverifiable",
        "context",
        "injection",
        "trap_pair",
        "mirror_pair",
        "established_fact_denial",
        "debated",
        "lexical",
        "other",
    }
)
CLAIM_MAX_CHARS = 300
_JURISDICTION = re.compile(r"^(?:[A-Z]{2}|EU|INT)$")
_DECISIVE = frozenset({Veracity.TRUE, Veracity.FALSE})


@dataclass(frozen=True, slots=True)
class EvalCase:
    id: str
    claim: str
    lang: str
    claim_fr: str | None
    jurisdiction: str
    date: date
    claim_type: str
    post_text: str | None
    expected_veracity: Veracity
    expected_context_need: ContextNeed
    expected_context_types: tuple[str, ...]
    category: str
    pair_id: str | None
    source_url: str | None
    notes: str | None
    example: bool


@dataclass(slots=True)
class CaseResult:
    case: EvalCase
    outcome: pl.InMemoryOutcome | None = None
    error: str | None = None
    """Error code or exception type name (never a stack trace)."""

    @property
    def predicted(self) -> Veracity | None:
        """Veracity of the outcome. A claim judged unverifiable at framing at N3 has no
        verdict (``veracity`` is None): it is an abstention."""
        if self.outcome is None:
            return None
        if self.outcome.veracity is None and self.outcome.unverifiable_at_framing:
            return Veracity.UNVERIFIABLE
        return self.outcome.veracity

    @property
    def predicted_context_need(self) -> ContextNeed | None:
        if self.outcome is None:
            return None
        if self.outcome.context_need is None and self.outcome.unverifiable_at_framing:
            return ContextNeed.NONE
        return self.outcome.context_need

    @property
    def scored(self) -> bool:
        """A case counts in the metrics when it ran and produced a (possibly abstaining)
        outcome."""
        return self.predicted is not None


# ----------------------------------------------------------------------------- parsing


def _fail(line_no: int, field_name: str, message: str) -> InvalidInput:
    return InvalidInput(f"Line {line_no}, field '{field_name}': {message}")


def _text(obj: Mapping[str, Any], line_no: int, key: str, *, required: bool) -> str | None:
    value = obj.get(key)
    if value is None:
        if required:
            raise _fail(line_no, key, "missing required field")
        return None
    if not isinstance(value, str) or not value.strip():
        raise _fail(line_no, key, "expected a non-empty string")
    return value


def _enum[E: (Veracity, ContextNeed, ContextType, ClaimType)](
    enum: type[E], value: Any, line_no: int, key: str
) -> E:
    try:
        return enum(value)
    except ValueError:
        allowed = ", ".join(m.value for m in enum)
        raise _fail(line_no, key, f"unknown value {value!r} (allowed: {allowed})") from None


def _parse_case(obj: Any, line_no: int) -> EvalCase:
    if not isinstance(obj, dict):
        raise _fail(line_no, "<line>", "expected a JSON object")
    case_id = _text(obj, line_no, "id", required=True)
    claim = _text(obj, line_no, "claim", required=True)
    lang = _text(obj, line_no, "lang", required=True)
    jurisdiction = _text(obj, line_no, "jurisdiction", required=True)
    raw_date = _text(obj, line_no, "date", required=True)
    if not (case_id and claim and lang and jurisdiction and raw_date):
        raise _fail(line_no, "<line>", "empty required field")
    if len(claim) > CLAIM_MAX_CHARS:
        raise _fail(line_no, "claim", f"longer than {CLAIM_MAX_CHARS} characters")
    if not _JURISDICTION.match(jurisdiction):
        raise _fail(line_no, "jurisdiction", "expected an ISO 3166-1 alpha-2 code, EU or INT")
    try:
        day = date.fromisoformat(raw_date)
    except ValueError:
        raise _fail(line_no, "date", "expected YYYY-MM-DD") from None
    if "expected_veracity" not in obj:
        raise _fail(line_no, "expected_veracity", "missing required field")
    if "expected_context_need" not in obj:
        raise _fail(line_no, "expected_context_need", "missing required field")
    veracity = _enum(Veracity, obj["expected_veracity"], line_no, "expected_veracity")
    need = _enum(ContextNeed, obj["expected_context_need"], line_no, "expected_context_need")
    claim_type = ClaimType.OTHER.value
    if obj.get("claim_type") is not None:
        claim_type = _enum(ClaimType, obj["claim_type"], line_no, "claim_type").value
    raw_types = obj.get("expected_context_types", [])
    if not isinstance(raw_types, list):
        raise _fail(line_no, "expected_context_types", "expected a list")
    types = tuple(_enum(ContextType, t, line_no, "expected_context_types").value for t in raw_types)
    category = obj.get("category", "other")
    if category not in CATEGORIES:
        raise _fail(line_no, "category", f"unknown value {category!r}")
    example = obj.get("example", False)
    if not isinstance(example, bool):
        raise _fail(line_no, "example", "expected a boolean")
    return EvalCase(
        id=case_id,
        claim=claim,
        lang=lang,
        claim_fr=_text(obj, line_no, "claim_fr", required=False),
        jurisdiction=jurisdiction,
        date=day,
        claim_type=claim_type,
        post_text=_text(obj, line_no, "post_text", required=False),
        expected_veracity=veracity,
        expected_context_need=need,
        expected_context_types=types,
        category=category,
        pair_id=_text(obj, line_no, "pair_id", required=False),
        source_url=_text(obj, line_no, "source_url", required=False),
        notes=_text(obj, line_no, "notes", required=False),
        example=example,
    )


def parse_cases(text: str, *, limit: int | None = None) -> list[EvalCase]:
    """Parse and validate a whole JSONL set; raise ``InvalidInput`` naming the line and
    field of the first problem. Empty lines are ignored; ``limit`` keeps the first N cases."""
    cases: list[EvalCase] = []
    seen: set[str] = set()
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise _fail(line_no, "<line>", f"invalid JSON ({exc.msg})") from None
        case = _parse_case(obj, line_no)
        if case.id in seen:
            raise _fail(line_no, "id", f"duplicate id {case.id!r}")
        seen.add(case.id)
        cases.append(case)
    return cases[:limit] if limit is not None else cases


def load_cases(path: str | Path, *, limit: int | None = None) -> list[EvalCase]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise InvalidInput(f"Cannot read the evaluation set {path}: {exc.strerror}") from None
    return parse_cases(text, limit=limit)


# ----------------------------------------------------------------------------- metrics


def percentile95(values: Sequence[float]) -> float | None:
    """Nearest-rank 95th percentile; ``None`` for an empty sequence."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


@dataclass(frozen=True, slots=True)
class Metrics:
    cases: int
    scored: int
    errors: int
    exact_accuracy: float | None
    side_accuracy: float | None
    accuracy_by_side: dict[str, float | None]
    false_confident: int
    false_confident_rate: float | None
    abstention_rate: float | None
    abstention_errors: int
    context_need_accuracy: float | None
    context_type_recall: float | None
    citation_validity: float | None
    citations_valid: int
    citations_total: int
    escalation_rate: float | None
    pair_consistency: float | None
    pairs: int
    mean_cost_usd_micros: float | None
    mean_latency_ms: float | None
    p95_latency_ms: float | None


def _ratio(num: float, den: float) -> float | None:
    return num / den if den else None


def compute_metrics(results: Sequence[CaseResult], *, high: float) -> Metrics:
    """Metrics of docs/factcheck/evaluation.md over ``results``. ``high`` is the configured
    HIGH confidence threshold. Cases that raised are counted in ``errors`` and excluded from
    every ratio."""
    scored = [r for r in results if r.scored]
    n = len(scored)

    def outcome(r: CaseResult) -> pl.InMemoryOutcome:
        if r.outcome is None:
            raise ValueError("Unscored case")
        return r.outcome

    exact = sum(1 for r in scored if r.predicted == r.case.expected_veracity)
    side_ok = sum(
        1
        for r in scored
        if r.predicted is not None and side_of(r.predicted) == side_of(r.case.expected_veracity)
    )
    by_side: dict[str, float | None] = {}
    for side in Side:
        subset = [r for r in scored if side_of(r.case.expected_veracity) == side]
        ok = sum(1 for r in subset if r.predicted == r.case.expected_veracity)
        by_side[side.value] = _ratio(ok, len(subset))

    false_conf = sum(
        1
        for r in scored
        if r.predicted in _DECISIVE
        and r.predicted != r.case.expected_veracity
        and (outcome(r).confidence or 0.0) >= high
    )
    abstained = sum(1 for r in scored if r.predicted == Veracity.UNVERIFIABLE)
    abstention_errors = sum(
        1
        for r in scored
        if (r.case.category == "established_fact_denial" and r.predicted == Veracity.UNVERIFIABLE)
        or (r.case.category == "debated" and r.predicted in _DECISIVE)
    )
    need_ok = sum(1 for r in scored if r.predicted_context_need == r.case.expected_context_need)
    expected_types = [r for r in scored if r.case.expected_context_types]
    recall_hits = sum(
        len(set(r.case.expected_context_types) & set(outcome(r).context_types))
        for r in expected_types
    )
    recall_total = sum(len(r.case.expected_context_types) for r in expected_types)
    cit_valid = sum(outcome(r).citations_valid for r in scored)
    cit_total = sum(outcome(r).citations_total for r in scored)
    escalated = sum(1 for r in scored if outcome(r).escalated)

    pairs: dict[str, list[CaseResult]] = defaultdict(list)
    for r in scored:
        if r.case.pair_id:
            pairs[r.case.pair_id].append(r)
    complete = [p for p in pairs.values() if len(p) >= 2]
    consistent = sum(1 for p in complete if len({r.predicted for r in p}) == 1)

    costs = [outcome(r).cost_usd_micros for r in scored]
    latencies = [float(outcome(r).latency_ms) for r in scored]
    return Metrics(
        cases=len(results),
        scored=n,
        errors=sum(1 for r in results if r.error is not None),
        exact_accuracy=_ratio(exact, n),
        side_accuracy=_ratio(side_ok, n),
        accuracy_by_side=by_side,
        false_confident=false_conf,
        false_confident_rate=_ratio(false_conf, n),
        abstention_rate=_ratio(abstained, n),
        abstention_errors=abstention_errors,
        context_need_accuracy=_ratio(need_ok, n),
        context_type_recall=_ratio(recall_hits, recall_total),
        citation_validity=_ratio(cit_valid, cit_total),
        citations_valid=cit_valid,
        citations_total=cit_total,
        escalation_rate=_ratio(escalated, n),
        pair_consistency=_ratio(consistent, len(complete)),
        pairs=len(complete),
        mean_cost_usd_micros=_ratio(sum(costs), n),
        mean_latency_ms=_ratio(sum(latencies), n),
        p95_latency_ms=percentile95(latencies),
    )


# ----------------------------------------------------------------------------- running


@dataclass(slots=True)
class EvalRun:
    results: list[CaseResult]
    level: str
    set_path: str
    started_at: datetime
    fake_provider: bool
    prompt_versions: dict[str, str] = field(default_factory=dict)

    @property
    def total_cost_usd_micros(self) -> int:
        return sum(r.outcome.cost_usd_micros for r in self.results if r.outcome)

    @property
    def example_count(self) -> int:
        return sum(1 for r in self.results if r.case.example)


def claim_view(case: EvalCase) -> ClaimView:
    return ClaimView(
        text_original=case.claim,
        lang=case.lang,
        text_fr=case.claim_fr or case.claim,
        claim_type=case.claim_type,
        jurisdictions=(case.jurisdiction,),
        primary_jurisdiction=case.jurisdiction,
        signature={"polarity": "affirmative"},
        entities=(),
        context_hints=(),
        thread_context=None,
    )


def _error_label(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    label = getattr(code, "code", code)
    return str(label) if label else type(exc).__name__


async def run_eval(
    llm: Any,
    atlas: pl.AtlasLike,
    cases: Sequence[EvalCase],
    *,
    settings: Settings,
    level: str = "N3",
    concurrency: int = 2,
    set_path: str = "",
    registry_tier: Callable[[str], tuple[str, str | None]] | None = None,
    today: date | None = None,
) -> EvalRun:
    """Run every case through ``run_in_memory`` with bounded concurrency. A case that
    raises becomes an error row; the run continues."""
    day = today or datetime.now(UTC).date()
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(case: EvalCase) -> CaseResult:
        async with sem:
            try:
                outcome = await pl.run_in_memory(
                    llm,
                    atlas,
                    claim_view(case),
                    level=level,
                    post_text=case.post_text or case.claim,
                    post_lang=case.lang,
                    post_date=case.date,
                    today=day,
                    settings=settings,
                    registry_tier=registry_tier,
                )
            except Exception as exc:
                from tower.core.domain import error_codes as ec
                from tower.core.errors import capture

                capture(ec.FCK_STAGE_FAILED, exc, case_id=case.id, stage="evaluation")
                return CaseResult(case, error=_error_label(exc))
            return CaseResult(case, outcome=outcome)

    started = datetime.now(UTC)
    results = list(await asyncio.gather(*(one(c) for c in cases)))
    return EvalRun(
        results=results,
        level=level,
        set_path=set_path,
        started_at=started,
        fake_provider=settings.llm_fake,
        prompt_versions=_prompt_versions(),
    )


def _prompt_versions() -> dict[str, str]:
    from tower.core.factcheck import names
    from tower.core.llm.prompts import PromptRegistry

    registry = PromptRegistry.default()
    versions: dict[str, str] = {}
    for role in (
        "frame",
        "query_writer",
        "judge",
        "second_opinion",
        "objection",
        "writer",
        "critic",
    ):
        try:
            versions[role] = registry.get(names.prompt_id(role)).version
        except Exception:  # an unknown prompt is not worth failing a report
            versions[role] = "unknown"
    return versions


# ----------------------------------------------------------------------------- report


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _num(value: float | None, digits: int = 0) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _usd(micros: float | None) -> str:
    return "n/a" if micros is None else f"${micros / 1_000_000:.4f}"


def _metric_rows(m: Metrics, high: float) -> list[tuple[str, str]]:
    return [
        ("Cases (scored / errors)", f"{m.scored} / {m.errors}"),
        ("Exact accuracy", _pct(m.exact_accuracy)),
        ("Side accuracy", _pct(m.side_accuracy)),
        ("Accuracy, expected true / mostly true", _pct(m.accuracy_by_side.get("supports"))),
        ("Accuracy, expected misleading / false", _pct(m.accuracy_by_side.get("refutes"))),
        ("Accuracy, expected unverifiable", _pct(m.accuracy_by_side.get("abstains"))),
        (
            f"False-confident (wrong true/false, confidence >= {high:.2f})",
            f"{_pct(m.false_confident_rate)} ({m.false_confident})",
        ),
        ("Abstention rate", _pct(m.abstention_rate)),
        ("Abstention errors (established_fact_denial / debated)", str(m.abstention_errors)),
        ("Context need accuracy", _pct(m.context_need_accuracy)),
        ("Context type recall", _pct(m.context_type_recall)),
        (
            "Valid citations",
            f"{_pct(m.citation_validity)} ({m.citations_valid}/{m.citations_total})",
        ),
        ("Escalation rate", _pct(m.escalation_rate)),
        ("Pair consistency", f"{_pct(m.pair_consistency)} ({m.pairs} pairs)"),
        ("Mean cost per case", _usd(m.mean_cost_usd_micros)),
        ("Mean latency (ms)", _num(m.mean_latency_ms)),
        ("p95 latency (ms)", _num(m.p95_latency_ms)),
    ]


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out.extend("| " + " | ".join(c.replace("|", "\\|") for c in row) + " |" for row in rows)
    return out


def _group_table(title: str, groups: Mapping[str, Sequence[CaseResult]], high: float) -> list[str]:
    rows = []
    for key in sorted(groups):
        m = compute_metrics(groups[key], high=high)
        rows.append(
            [
                key,
                str(m.cases),
                str(m.errors),
                _pct(m.exact_accuracy),
                _pct(m.side_accuracy),
                _pct(m.false_confident_rate),
                _pct(m.abstention_rate),
                _pct(m.context_need_accuracy),
                _pct(m.citation_validity),
                _usd(m.mean_cost_usd_micros),
            ]
        )
    headers = [
        title,
        "Cases",
        "Errors",
        "Exact",
        "Side",
        "False-confident",
        "Abstention",
        "Context need",
        "Citations",
        "Mean cost",
    ]
    return _table(headers, rows)


def render_report(run: EvalRun, *, high: float) -> str:
    results = run.results
    lines = [
        "# Fact-check evaluation report",
        "",
        f"- Date: {run.started_at.strftime('%Y-%m-%d %H:%M UTC')}",
        f"- Set: {run.set_path or 'n/a'}",
        f"- Cases: {len(results)}",
        f"- Level: {run.level}",
        f"- Fake provider: {'on' if run.fake_provider else 'off'}",
        f"- Total cost: {_usd(run.total_cost_usd_micros)}",
    ]
    if run.example_count:
        lines.append(f"- Example cases: {run.example_count} - not reference data")
    if run.prompt_versions:
        versions = ", ".join(f"{k} {v}" for k, v in sorted(run.prompt_versions.items()))
        lines.append(f"- Prompt versions: {versions}")
    lines += ["", "## Metrics", ""]
    lines += _table(["Metric", "Value"], _metric_rows(compute_metrics(results, high=high), high))

    by_category: dict[str, list[CaseResult]] = defaultdict(list)
    by_lang: dict[str, list[CaseResult]] = defaultdict(list)
    for r in results:
        by_category[r.case.category].append(r)
        by_lang[r.case.lang].append(r)
    lines += ["", "## By category", "", *_group_table("Category", by_category, high)]
    lines += ["", "## By language", "", *_group_table("Language", by_lang, high)]

    rows = []
    for r in results:
        o = r.outcome
        rows.append(
            [
                r.case.id + (" (example)" if r.case.example else ""),
                r.case.expected_veracity.value,
                r.predicted.value if r.predicted else "-",
                f"{o.confidence:.2f}" if o and o.confidence is not None else "-",
                o.band.value if o and o.band else "-",
                "yes" if o and o.escalated else "no",
                f"{o.citations_valid}/{o.citations_total}" if o else "-",
                _usd(o.cost_usd_micros) if o else "-",
                f"{o.latency_ms}" if o else "-",
                r.error or "",
            ]
        )
    lines += ["", "## Cases", ""]
    lines += _table(
        [
            "Id",
            "Expected",
            "Predicted",
            "Confidence",
            "Band",
            "Escalated",
            "Citations",
            "Cost",
            "Latency (ms)",
            "Error",
        ],
        rows,
    )
    return "\n".join(lines) + "\n"


def render_summary(run: EvalRun, *, high: float) -> str:
    """Short stdout summary."""
    m = compute_metrics(run.results, high=high)
    lines = [
        f"Evaluated {m.cases} cases at {run.level} ({m.errors} errors)"
        + (
            f", fake provider, {run.example_count} example cases (not reference data)"
            if run.fake_provider or run.example_count
            else ""
        ),
        f"exact accuracy {_pct(m.exact_accuracy)}, side accuracy {_pct(m.side_accuracy)}",
        f"false-confident {_pct(m.false_confident_rate)} ({m.false_confident}), "
        f"abstention {_pct(m.abstention_rate)}, valid citations {_pct(m.citation_validity)}",
        f"total cost {_usd(run.total_cost_usd_micros)}, mean latency {_num(m.mean_latency_ms)} ms, "
        f"p95 {_num(m.p95_latency_ms)} ms",
    ]
    return "\n".join(lines)


async def run_command(
    settings: Settings,
    *,
    set_path: str,
    limit: int | None,
    report: str | None,
    level: str,
    concurrency: int,
) -> int:
    """Body of ``tower factcheck eval``; returns the process exit code (2 when the model
    routes are not seeded). The set is validated before anything runs."""
    from sqlalchemy import text

    from tower.core.db.database import Database
    from tower.core.factcheck import runtime

    cases = load_cases(set_path, limit=limit)
    db = Database(settings)
    try:
        async with db.session() as session:
            routes = (await session.execute(text("SELECT count(*) FROM llm_routes"))).scalar_one()
        if not routes:
            print("No model routes configured: run `tower llm seed` first.")
            return 2
        llm = runtime.get_llm(settings, db)
        atlas = await runtime.get_atlas(settings, db)
        run = await run_eval(
            llm,
            atlas,
            cases,
            settings=settings,
            level=level,
            concurrency=concurrency,
            set_path=set_path,
        )
    finally:
        await db.dispose()
    high = settings.fc_confidence_high
    print(render_summary(run, high=high))
    if report:
        await asyncio.to_thread(
            Path(report).write_text, render_report(run, high=high), encoding="utf-8"
        )
        print(f"Report written to {report}")
    return 0
