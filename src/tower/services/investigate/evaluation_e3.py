"""E3 layer of ``tower factcheck eval``: claim matching, groups, value bands.

For every pair of cases listed in the set (``pair_id`` pairs, cases sharing a ``group``
label) this module re-runs the production E3 decision between the two claims: E2
extraction (same input builder, same faithfulness filter, cached by ``llm_calls``),
normalized signatures, embeddings, ``plan_match`` with the active profile thresholds and
the pair arbiter. Nothing is written except the caches (``llm_calls``, budgets).

It deliberately re-implements the small orchestration of ``services/factcheck`` on top of
``tower.core`` (services may not import each other). Everything except ``run_e3`` is pure
so the metrics are testable on synthetic decisions.
"""

from __future__ import annotations

import asyncio
import itertools
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from tower.core.config import Settings
from tower.core.domain.factcheck.bands import band_of, is_outdated, tolerance
from tower.core.domain.factcheck.datamark import datamark
from tower.core.domain.factcheck.fidelity import (
    check_claim_fidelity,
    check_omission,
    extract_numbers,
)
from tower.core.domain.factcheck.matching import Candidate, plan_match
from tower.core.domain.factcheck.profiles import Profile
from tower.core.domain.factcheck.signatures import NormalizedSignature, compare_signatures
from tower.core.domain.factcheck.values import AbsenceReason, ArbiterReason, Band, BandSide
from tower.core.domain.factcheck.vectors import cosine
from tower.core.domain.llm import Level
from tower.core.factcheck import inputs, names
from tower.core.factcheck.candidates import signature_from_extracted
from tower.core.factcheck.schemas import ExtractedClaim, ExtractOutput, PairArbiterOutput
from tower.core.llm.errors import LLMRouterError
from tower.core.ports.llm import LLMRequest

if TYPE_CHECKING:
    from tower.services.investigate.evaluation import EvalCase

__all__ = [
    "FALSE_REUSE_TARGET",
    "NO_PAID_SEARCH_TARGET",
    "BandDecision",
    "E3Metrics",
    "E3Report",
    "PairDecision",
    "PairSpec",
    "Reference",
    "compute_e3_metrics",
    "expected_pairs",
    "needs_e3",
    "render_e3_section",
    "run_e3",
]

FALSE_REUSE_TARGET = 0.01
NO_PAID_SEARCH_TARGET = 0.5
_EXTRACT_MAX_TOKENS = 3000
_ARBITER_MAX_TOKENS = 300
_PIVOT_LANG = "fr"
_REPLAY_NOTE = (
    "The previous extraction added a figure or entity absent from the post, or dropped "
    "the post's figures. Extract again, strictly from the post."
)
_SCALES = {"thousand": Decimal(1000), "million": Decimal(1_000_000), "billion": Decimal(10**9)}


@dataclass(frozen=True, slots=True)
class Reference:
    """Reference fact of a case, used to compute its value band."""

    value: Decimal
    unit: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    territory: str | None = None
    definition_variant: str | None = None
    previous_value: Decimal | None = None


@dataclass(frozen=True, slots=True)
class PairSpec:
    """Two cases compared by E3. ``a`` plays the group representative, ``b`` the new
    claim. ``expect`` is ``same``, ``different`` or ``None`` (compared, not scored)."""

    a: str
    b: str
    expect: str | None


@dataclass(frozen=True, slots=True)
class PairDecision:
    spec: PairSpec
    joined: bool
    rule: str
    similarity: float | None = None
    arbiter_calls: int = 0
    category: str = "other"
    langs: str = ""


@dataclass(frozen=True, slots=True)
class BandDecision:
    case_id: str
    band: Band
    side: BandSide
    outdated: bool
    expected_band: tuple[Band, BandSide] | None
    expected_outdated: bool | None


@dataclass(slots=True)
class E3Report:
    decisions: list[PairDecision] = field(default_factory=list)
    bands: list[BandDecision] = field(default_factory=list)
    labels: dict[str, str | None] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    """Case id or ``a|b`` pair key -> error label."""
    cost_usd_micros: int = 0
    claim_cases: int = 0


# ------------------------------------------------------------------------ pair planning


def _pair_expectation(a: EvalCase, b: EvalCase) -> str | None:
    if a.pair_id is not None and a.pair_id == b.pair_id:
        for case in (a, b):
            if case.pair_expect is not None:
                return case.pair_expect
        if "trap_pair" in (a.category, b.category):
            return "different"
        if "interlingual_pair" in (a.category, b.category):
            return "same"
    if a.group is not None and b.group is not None:
        return "same" if a.group == b.group else "different"
    return None


def expected_pairs(cases: Sequence[EvalCase]) -> list[PairSpec]:
    """Pairs to compare, in file order: every two cases of a ``pair_id`` and every two
    cases of a ``group`` label. Only pairs with an expectation are kept (a mirror pair
    without expectation is an E8 pair, not an E3 one). Never all N pairs."""
    by_key: dict[tuple[str, str], list[EvalCase]] = defaultdict(list)
    for case in cases:
        if case.pair_id:
            by_key["pair", case.pair_id].append(case)
        if case.group:
            by_key["group", case.group].append(case)
    seen: set[tuple[str, str]] = set()
    out: list[PairSpec] = []
    for members in by_key.values():
        for a, b in itertools.combinations(members, 2):
            key = (a.id, b.id)
            if key in seen:
                continue
            seen.add(key)
            expect = _pair_expectation(a, b)
            if expect is not None:
                out.append(PairSpec(a.id, b.id, expect))
    order = {c.id: i for i, c in enumerate(cases)}
    out.sort(key=lambda p: (order[p.a], order[p.b]))
    return out


def needs_e3(cases: Sequence[EvalCase]) -> bool:
    return bool(expected_pairs(cases)) or any(c.reference is not None for c in cases)


# --------------------------------------------------------------------------- metrics


@dataclass(frozen=True, slots=True)
class E3Metrics:
    pairs: int
    pair_errors: int
    expected_different: int
    false_reuse: int
    false_reuse_rate: float | None
    wrong_merges: int
    wrong_merge_rate: float | None
    different_label_pairs: int
    missed_groupings: int
    missed_grouping_rate: float | None
    same_label_pairs: int
    group_purity: float | None
    interlingual_pairs: int
    interlingual_joined: int
    interlingual_rate: float | None
    arbiter_calls: int
    arbiter_share: float | None
    band_accuracy: float | None
    band_cases: int
    outdated_accuracy: float | None
    outdated_cases: int


def _ratio(num: float, den: float) -> float | None:
    return num / den if den else None


def _components(ids: Sequence[str], joins: Sequence[tuple[str, str]]) -> dict[str, str]:
    parent = {i: i for i in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in joins:
        if a in parent and b in parent:
            parent[find(a)] = find(b)
    return {i: find(i) for i in ids}


def predicted_groups(report: E3Report) -> dict[str, str]:
    """Union-find over the JOIN decisions: case id -> group representative id."""
    ids = sorted({c for d in report.decisions for c in (d.spec.a, d.spec.b)})
    joins = [(d.spec.a, d.spec.b) for d in report.decisions if d.joined]
    return _components(ids, joins)


def _label_pair(d: PairDecision, labels: Mapping[str, str | None]) -> bool | None:
    """True when both cases carry the same label, False when different, None otherwise."""
    la, lb = labels.get(d.spec.a), labels.get(d.spec.b)
    if la is None or lb is None:
        return None
    return la == lb


def compute_e3_metrics(report: E3Report) -> E3Metrics:
    decisions = report.decisions
    different = [d for d in decisions if d.spec.expect == "different"]
    false_reuse = sum(1 for d in different if d.joined)
    labelled = [(d, _label_pair(d, report.labels)) for d in decisions]
    diff_label = [d for d, same in labelled if same is False]
    same_label = [d for d, same in labelled if same is True]
    wrong = sum(1 for d in diff_label if d.joined)
    missed = sum(1 for d in same_label if not d.joined)

    groups = predicted_groups(report)
    members: dict[str, list[str]] = defaultdict(list)
    for case_id, root in groups.items():
        if report.labels.get(case_id) is not None:
            members[root].append(str(report.labels[case_id]))
    total = sum(len(v) for v in members.values())
    majority = sum(max(Counter(v).values()) for v in members.values())

    interlingual = [d for d in decisions if d.category == "interlingual_pair"]
    arbiter = sum(d.arbiter_calls for d in decisions)
    scored_bands = [b for b in report.bands if b.expected_band is not None]
    scored_outdated = [b for b in report.bands if b.expected_outdated is not None]
    return E3Metrics(
        pairs=len(decisions),
        pair_errors=sum(1 for k in report.errors if "|" in k),
        expected_different=len(different),
        false_reuse=false_reuse,
        false_reuse_rate=_ratio(false_reuse, len(different)),
        wrong_merges=wrong,
        wrong_merge_rate=_ratio(wrong, len(diff_label)),
        different_label_pairs=len(diff_label),
        missed_groupings=missed,
        missed_grouping_rate=_ratio(missed, len(same_label)),
        same_label_pairs=len(same_label),
        group_purity=_ratio(majority, total),
        interlingual_pairs=len(interlingual),
        interlingual_joined=sum(1 for d in interlingual if d.joined),
        interlingual_rate=_ratio(sum(1 for d in interlingual if d.joined), len(interlingual)),
        arbiter_calls=arbiter,
        arbiter_share=_ratio(arbiter, len(decisions)),
        band_accuracy=_ratio(
            sum(1 for b in scored_bands if (b.band, b.side) == b.expected_band), len(scored_bands)
        ),
        band_cases=len(scored_bands),
        outdated_accuracy=_ratio(
            sum(1 for b in scored_outdated if b.outdated == b.expected_outdated),
            len(scored_outdated),
        ),
        outdated_cases=len(scored_outdated),
    )


# ------------------------------------------------------------------------- rendering


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _usd(micros: float | None) -> str:
    return "n/a" if micros is None else f"${micros / 1_000_000:.4f}"


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out.extend("| " + " | ".join(c.replace("|", "\\|") for c in row) + " |" for row in rows)
    return out


def false_reuse_mark(rate: float | None) -> str:
    if rate is None:
        return "n/a"
    return f"{_pct(rate)} (target < {FALSE_REUSE_TARGET * 100:.0f}%) " + (
        "✅" if rate < FALSE_REUSE_TARGET else "❌"
    )


def _breakdown(title: str, groups: Mapping[str, Sequence[PairDecision]]) -> list[str]:
    rows = []
    for key in sorted(groups):
        ds = groups[key]
        diff = [d for d in ds if d.spec.expect == "different"]
        same = [d for d in ds if d.spec.expect == "same"]
        rows.append(
            [
                key,
                str(len(ds)),
                str(sum(1 for d in diff if d.joined)),
                _pct(_ratio(sum(1 for d in diff if d.joined), len(diff))),
                str(sum(1 for d in same if not d.joined)),
                _pct(_ratio(sum(1 for d in same if d.joined), len(same))),
            ]
        )
    return _table([title, "Pairs", "False reuse", "False reuse rate", "Missed", "Join rate"], rows)


def render_e3_section(report: E3Report) -> list[str]:
    """Markdown lines of the E3 section (headline metric first)."""
    m = compute_e3_metrics(report)
    lines = ["## E3: matching, groups and bands", ""]
    lines.append(
        f"**False reuse rate: {false_reuse_mark(m.false_reuse_rate)}** "
        f"({m.false_reuse} / {m.expected_different} pairs expected different)"
    )
    lines.append("")
    rows = [
        ("Pairs compared (errors)", f"{m.pairs} ({m.pair_errors})"),
        (
            "Wrong merges (JOIN between different labels)",
            f"{_pct(m.wrong_merge_rate)} ({m.wrong_merges} / {m.different_label_pairs})",
        ),
        (
            "Missed groupings (NEW between same label)",
            f"{_pct(m.missed_grouping_rate)} ({m.missed_groupings} / {m.same_label_pairs})",
        ),
        ("Group purity", _pct(m.group_purity)),
        (
            "Interlingual pairs joined (target: all)",
            f"{_pct(m.interlingual_rate)} ({m.interlingual_joined} / {m.interlingual_pairs})",
        ),
        (
            "Arbiter calls",
            f"{m.arbiter_calls} ({_pct(m.arbiter_share)} of pairs)",
        ),
        ("E3 cost (extraction, embeddings, arbiter)", _usd(report.cost_usd_micros)),
        ("Band accuracy", f"{_pct(m.band_accuracy)} ({m.band_cases} cases)"),
        ("Outdated accuracy", f"{_pct(m.outdated_accuracy)} ({m.outdated_cases} cases)"),
    ]
    lines += _table(["Metric", "Value"], rows)
    by_category: dict[str, list[PairDecision]] = defaultdict(list)
    by_langs: dict[str, list[PairDecision]] = defaultdict(list)
    for d in report.decisions:
        by_category[d.category].append(d)
        by_langs[d.langs].append(d)
    if report.decisions:
        lines += ["", "### E3 by category", "", *_breakdown("Category", by_category)]
        lines += ["", "### E3 by language", "", *_breakdown("Languages", by_langs)]
        lines += ["", "### Pairs", ""]
        lines += _table(
            ["A", "B", "Expected", "Decision", "Rule", "Similarity", "Arbiter calls"],
            [
                [
                    d.spec.a,
                    d.spec.b,
                    d.spec.expect or "-",
                    "JOIN" if d.joined else "NEW",
                    d.rule,
                    "-" if d.similarity is None else f"{d.similarity:.3f}",
                    str(d.arbiter_calls),
                ]
                for d in report.decisions
            ],
        )
    if report.bands:
        lines += ["", "### Bands", ""]
        lines += _table(
            ["Case", "Band", "Side", "Outdated", "Expected band", "Expected outdated"],
            [
                [
                    b.case_id,
                    b.band.value,
                    b.side.value,
                    "yes" if b.outdated else "no",
                    f"{b.expected_band[0].value}/{b.expected_band[1].value}"
                    if b.expected_band
                    else "-",
                    "-" if b.expected_outdated is None else str(b.expected_outdated).lower(),
                ]
                for b in report.bands
            ],
        )
    if report.errors:
        lines += ["", "### E3 errors", ""]
        lines += _table(["Case or pair", "Error"], sorted(report.errors.items()))
    return lines


# ------------------------------------------------------------------------- extraction


def _fmt(value: float) -> str:
    return format(Decimal(repr(value)).normalize(), "f")


def _covered(value: float, available: frozenset[str], scale: str | None) -> bool:
    v = Decimal(repr(value))
    factors = [Decimal(1)]
    if scale in _SCALES:
        factors.append(_SCALES[str(scale)])
    for number in available:
        base = Decimal(number)
        half = Decimal("0.5") * Decimal(10) ** -(len(number.split(".")[1]) if "." in number else 0)
        if any(abs(v - base * f) <= half * f for f in factors):
            return True
    return False


def _values(claim: ExtractedClaim) -> list[float]:
    sig = claim.signature
    return [v for v in (sig.value_low, sig.value_high) if v is not None]


def _faithful(output: ExtractOutput, post_text: str) -> tuple[list[ExtractedClaim], bool]:
    """Same contamination and omission checks as ``services/factcheck`` E2 (without the
    error capture: an evaluation must not write ``error_reports``)."""
    available = extract_numbers(post_text)
    kept: list[ExtractedClaim] = []
    failed = False
    for claim in output.claims:
        extra = [
            _fmt(v)
            for v in _values(claim)
            if not _covered(v, available, claim.signature.unit_scale)
        ]
        text = " ".join([claim.text_original, *extra])
        if check_claim_fidelity(text, [e.name for e in claim.entities], post_text):
            failed = True
            continue
        kept.append(claim)
    has_absence = output.absence_reason not in (None, AbsenceReason.NO_CLAIM)
    full = [" ".join([c.text_original, *(_fmt(v) for v in _values(c))]) for c in output.claims]
    if check_omission(post_text, full, has_absence=has_absence):
        failed = True
    return kept, failed


@dataclass(slots=True)
class _Prepared:
    case: EvalCase
    claim: ExtractedClaim
    signature: NormalizedSignature
    vector: list[float]
    pivot: list[float]


class _CaseFailed(Exception):
    def __init__(self, label: str) -> None:
        super().__init__(label)
        self.label = label


async def _extract_claim(
    llm: Any, case: EvalCase, *, level: str, today: date
) -> tuple[ExtractedClaim, int]:
    post_text = case.post_text or case.claim
    created = datetime(case.date.year, case.date.month, case.date.day, tzinfo=UTC)
    payload = inputs.extract_input(
        post_id=case.id, lang=case.lang, text=post_text, created_at=created, today=today
    )
    cost = 0

    async def once(model_input: dict[str, Any]) -> ExtractOutput:
        nonlocal cost
        request = LLMRequest(
            role=names.ROLE_EXTRACT,
            level=Level(level),
            prompt_id=names.prompt_id(names.ROLE_EXTRACT),
            input=model_input,
            schema=ExtractOutput,
            max_output_tokens=_EXTRACT_MAX_TOKENS,
            budget_scope=names.level_scope(level),
        )
        result = await llm.complete(request)
        cost += result.cost_usd_micros
        if not isinstance(result.output, ExtractOutput):
            raise _CaseFailed("unexpected_extraction_output")
        if result.output.post_id != case.id:
            raise _CaseFailed("extraction_post_id_mismatch")
        return result.output

    output = await once(payload)
    claims, failed = _faithful(output, post_text)
    if failed:
        claims, _ = _faithful(await once({**payload, "replay": _REPLAY_NOTE}), post_text)
    if not claims:
        raise _CaseFailed("no_claim_extracted")
    return claims[0], cost


async def _prepare(
    llm: Any, embedder: Any, case: EvalCase, *, level: str, today: date
) -> tuple[_Prepared, int]:
    claim, cost = await _extract_claim(llm, case, level=level, today=today)
    texts = [claim.text_original]
    french = case.lang.casefold() == _PIVOT_LANG
    if not french and claim.text_fr not in texts:
        texts.append(claim.text_fr)
    result = await embedder.embed(texts, level=level, ref=f"eval:{case.id}")
    cost += result.cost_usd_micros
    vector = result.vectors[0]
    pivot = vector if french else result.vectors[texts.index(claim.text_fr)]
    signature = signature_from_extracted(claim, case.date)
    return _Prepared(case, claim, signature, vector, pivot), cost


# --------------------------------------------------------------------------- deciding


async def _arbiter_same(
    llm: Any, new: _Prepared, rep: _Prepared, *, level: str
) -> tuple[bool, int]:
    """One ``pair_arbiter`` call on the two original texts. Honoured only for ``same``
    with reason ``same_claim``; any router failure is "different" (budget propagates)."""
    from tower.core.llm.errors import BudgetExhausted

    request = LLMRequest(
        role=names.ROLE_PAIR_ARBITER,
        level=Level(level),
        prompt_id=names.prompt_id(names.ROLE_PAIR_ARBITER),
        input=inputs.pair_arbiter_input(
            a_text=datamark(new.claim.text_original),
            a_lang=new.case.lang,
            a_date=new.case.date,
            b_text=datamark(rep.claim.text_original),
            b_lang=rep.case.lang,
            b_date=rep.case.date,
        ),
        schema=PairArbiterOutput,
        max_output_tokens=_ARBITER_MAX_TOKENS,
        budget_scope=names.level_scope(level),
    )
    try:
        result = await llm.complete(request)
    except BudgetExhausted:
        raise
    except LLMRouterError:
        return False, 0
    output = result.output
    same = (
        isinstance(output, PairArbiterOutput)
        and output.same
        and output.reason is ArbiterReason.SAME_CLAIM
    )
    return same, result.cost_usd_micros


async def decide_pair(
    llm: Any,
    rep: _Prepared,
    new: _Prepared,
    *,
    settings: Settings,
    profile: Profile,
    level: str,
) -> tuple[bool, str, float | None, int, int]:
    """JOIN or NEW between ``new`` and the representative ``rep``, as production E3 does
    with a single candidate group. Returns (joined, rule, similarity, arbiter calls, cost)."""
    same_lang = rep.case.lang.casefold() == new.case.lang.casefold()
    pivot_sim = cosine(new.pivot, rep.pivot)
    original_sim = cosine(new.vector, rep.vector) if same_lang else None
    similarity = original_sim if original_sim is not None else pivot_sim
    candidate = Candidate(
        group_id=0,
        claim_lang=rep.case.lang,
        similarity_pivot=pivot_sim,
        similarity_original=original_sim,
        comparison=compare_signatures(
            new.signature, rep.signature, current_period_days=settings.fc_current_period_days
        ),
        has_active_verdict=False,
    )
    plan = plan_match(
        new.case.lang,
        [candidate],
        same_threshold=profile.match_same_threshold,
        grey_threshold=profile.match_grey_threshold,
    )
    if plan.join_without_arbiter is not None:
        return True, "identical_signature:similarity", similarity, 0, 0
    calls = cost = 0
    if plan.arbiter_queue:
        same, spent = await _arbiter_same(llm, new, rep, level=level)
        calls, cost = calls + 1, cost + spent
        if same:
            return True, "identical_signature:arbiter", similarity, calls, cost
        return False, "new:arbiter_different", similarity, calls, cost
    if plan.related_candidates:
        same, spent = await _arbiter_same(llm, new, rep, level=level)
        calls, cost = calls + 1, cost + spent
        return False, ("new:related" if same else "new:arbiter_different"), similarity, calls, cost
    first = next(iter(plan.rejected.values()), "no_match")
    return False, f"new:{first}", similarity, 0, 0


def band_decision(
    case: EvalCase, signature: NormalizedSignature, settings: Settings
) -> BandDecision:
    """Band, side and outdated flag of the claimed interval against the case reference
    (production ``band_for`` without the database). The unit of the reference stands in
    when the extraction did not set one."""
    ref = case.reference
    if ref is None:
        raise ValueError("Case without reference")
    tau = tolerance(
        ref.value,
        unit=signature.unit or ref.unit,
        ratio=Decimal(str(settings.fc_band_tau_ratio)),
        floor_rate_points=Decimal(str(settings.fc_band_floor_rate_points)),
        floor_default=Decimal(str(settings.fc_band_floor_default)),
    )
    band, side = band_of(signature.value_low, signature.value_high, ref.value, tau)
    outdated = is_outdated(
        signature.value_low,
        signature.value_high,
        current=ref.value,
        previous=ref.previous_value,
        tau=tau,
    )
    return BandDecision(case.id, band, side, outdated, case.expected_band, case.expected_outdated)


async def run_e3(
    llm: Any,
    embedder: Any,
    cases: Sequence[EvalCase],
    *,
    settings: Settings,
    profile: Profile,
    level: str = "N3",
    concurrency: int = 2,
    today: date | None = None,
) -> E3Report:
    """Run the E3 layer over the pairs and reference cases of ``cases``."""
    day = today or datetime.now(UTC).date()
    pairs = expected_pairs(cases)
    by_id = {c.id: c for c in cases}
    needed: list[str] = []
    for spec in pairs:
        needed += [spec.a, spec.b]
    needed += [c.id for c in cases if c.reference is not None]
    needed = list(dict.fromkeys(needed))
    report = E3Report(labels={c.id: c.group for c in cases})
    sem = asyncio.Semaphore(max(1, concurrency))
    prepared: dict[str, _Prepared] = {}
    costs: dict[str, int] = {}

    async def prepare(case_id: str) -> None:
        async with sem:
            try:
                prepared[case_id], costs[case_id] = await _prepare(
                    llm, embedder, by_id[case_id], level=level, today=day
                )
            except _CaseFailed as exc:
                report.errors[case_id] = exc.label
            except Exception as exc:  # one case never stops the run
                code = getattr(exc, "code", None)
                report.errors[case_id] = str(getattr(code, "code", code) or type(exc).__name__)

    await asyncio.gather(*(prepare(i) for i in needed))
    report.claim_cases = len(prepared)
    report.cost_usd_micros = sum(costs.values())

    for spec in pairs:
        key = f"{spec.a}|{spec.b}"
        a, b = prepared.get(spec.a), prepared.get(spec.b)
        if a is None or b is None:
            report.errors[key] = "case_failed"
            continue
        try:
            joined, rule, sim, calls, spent = await decide_pair(
                llm, a, b, settings=settings, profile=profile, level=level
            )
        except Exception as exc:
            code = getattr(exc, "code", None)
            report.errors[key] = str(getattr(code, "code", code) or type(exc).__name__)
            continue
        report.cost_usd_micros += spent
        category = by_id[spec.b].category
        if by_id[spec.a].category != "other" and category == "other":
            category = by_id[spec.a].category
        report.decisions.append(
            PairDecision(
                spec,
                joined,
                rule,
                sim,
                calls,
                category=category,
                langs=f"{by_id[spec.a].lang}-{by_id[spec.b].lang}",
            )
        )
    for case in cases:
        got = prepared.get(case.id)
        if case.reference is not None and got is not None:
            report.bands.append(band_decision(case, got.signature, settings))
    return report
