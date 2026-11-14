"""Batch layer of ``tower factcheck eval --batch``: E2 micro-batch contamination and omission.

The cases' posts are composed into batches with the production pure composer
(``core.domain.factcheck.batching.compose``, vectors from the embedder), a canary post is
inserted in every batch, role ``extract_batch`` runs on the batch and role ``extract`` on
every post alone (the solo run is the reference; ``llm_calls`` caches make replays free).
Nothing is written except the caches. Everything except ``run_batch_eval`` is pure so the
metrics are testable on synthetic outputs.
"""

from __future__ import annotations

import asyncio
import random
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tower.core.config import Settings
from tower.core.domain.factcheck import batching as dom
from tower.core.domain.factcheck.fidelity import extract_numbers
from tower.core.domain.llm import Level
from tower.core.factcheck import inputs, names
from tower.core.factcheck.schemas import ExtractBatchOutput, ExtractedClaim, ExtractOutput
from tower.core.llm.errors import LLMRouterError
from tower.core.ports.llm import LLMRequest

if TYPE_CHECKING:
    from tower.services.investigate.evaluation import EvalCase

__all__ = [
    "BatchEvalReport",
    "BatchMetrics",
    "BatchRun",
    "PostRun",
    "compute_batch_metrics",
    "render_batch_report",
    "render_batch_summary",
    "run_batch_command",
    "run_batch_eval",
]

LEVEL = "N1"
_SOLO_MAX_TOKENS = 3000
_PER_POST_OUTPUT_TOKENS = 700
_MAX_OUTPUT_TOKENS = 8000
_CONCURRENCY = 4


@dataclass(frozen=True, slots=True)
class PostRun:
    """One post of a batch: its text and the claims (full texts, figures included) of the
    batch result and of the solo reference."""

    case_id: str
    batch: int
    position: int
    """Position in the sent batch (the canary included), 0-based."""
    text: str
    batch_claims: tuple[str, ...]
    solo_claims: tuple[str, ...]
    returned: bool = True
    """False when the batch output has no entry for the post."""


@dataclass(frozen=True, slots=True)
class BatchRun:
    index: int
    case_ids: tuple[str, ...]
    canary_token: str | None
    canary_contaminated: bool
    size_sent: int
    cost_usd_micros: int


@dataclass(slots=True)
class BatchEvalReport:
    set_path: str
    batch_size: int
    batches: list[BatchRun] = field(default_factory=list)
    posts: list[PostRun] = field(default_factory=list)
    solo_cost_usd_micros: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    """``(case or batch label, reason)`` for what could not be run."""


@dataclass(frozen=True, slots=True)
class BatchMetrics:
    batches: int
    posts: int
    canary_batches_contaminated: int
    contaminated_posts: int
    contamination_rate: float | None
    omitted_posts: int
    omission_rate: float | None
    omission_by_position: dict[int, float]
    claims_delta: float | None
    """Mean of (batch claims - solo claims) per post."""
    batch_cost_per_post_usd_micros: float | None
    solo_cost_per_post_usd_micros: float | None


# ----------------------------------------------------------------------------- pure


def _ratio(num: float, den: float) -> float | None:
    return num / den if den else None


def _numbers(texts: Sequence[str]) -> frozenset[str]:
    return extract_numbers(" ".join(texts))


def claim_text(claim: ExtractedClaim) -> str:
    """Claim text followed by its signature values (so figures only in the signature are
    seen by the metrics)."""
    sig = claim.signature
    values = [v for v in (sig.value_low, sig.value_high) if v is not None]
    return " ".join(
        [claim.text_original, *(format(Decimal(repr(v)).normalize(), "f") for v in values)]
    )


def is_contaminated(post: PostRun, others: Sequence[PostRun], canary_token: str | None) -> bool:
    """A figure of the canary, or of another post of the batch that this post does not
    contain, appears in the post's batch claims."""
    if not post.batch_claims:
        return False
    if canary_token is not None and dom.canary_contaminated(
        canary_token, {post.case_id: post.batch_claims}
    ):
        return True
    own = extract_numbers(post.text)
    foreign: set[str] = set()
    for other in others:
        foreign |= extract_numbers(other.text)
    foreign -= own
    return bool(_numbers(post.batch_claims) & foreign)


def is_omitted(post: PostRun) -> bool:
    """The solo run has a figure that the batch result misses."""
    return bool(_numbers(post.solo_claims) - _numbers(post.batch_claims))


def compute_batch_metrics(report: BatchEvalReport) -> BatchMetrics:
    by_batch: dict[int, list[PostRun]] = defaultdict(list)
    for post in report.posts:
        by_batch[post.batch].append(post)
    tokens = {b.index: b.canary_token for b in report.batches}
    contaminated = sum(
        is_contaminated(
            post,
            [o for o in by_batch[post.batch] if o.case_id != post.case_id],
            tokens.get(post.batch),
        )
        for post in report.posts
    )
    omitted_flags = [(post, is_omitted(post)) for post in report.posts]
    omitted = sum(flag for _, flag in omitted_flags)
    positions: dict[int, list[bool]] = defaultdict(list)
    for post, flag in omitted_flags:
        positions[post.position].append(flag)
    n = len(report.posts)
    delta = _ratio(sum(len(p.batch_claims) - len(p.solo_claims) for p in report.posts), n)
    return BatchMetrics(
        batches=len(report.batches),
        posts=n,
        canary_batches_contaminated=sum(b.canary_contaminated for b in report.batches),
        contaminated_posts=contaminated,
        contamination_rate=_ratio(contaminated, n),
        omitted_posts=omitted,
        omission_rate=_ratio(omitted, n),
        omission_by_position={
            pos: sum(flags) / len(flags) for pos, flags in sorted(positions.items())
        },
        claims_delta=delta,
        batch_cost_per_post_usd_micros=_ratio(sum(b.cost_usd_micros for b in report.batches), n),
        solo_cost_per_post_usd_micros=_ratio(report.solo_cost_usd_micros, n),
    )


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _delta(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.2f}"


def _micros(value: float | None) -> str:
    return "n/a" if value is None else f"{value / 1_000_000:.6f} $"


def render_batch_summary(report: BatchEvalReport) -> str:
    m = compute_batch_metrics(report)
    lines = [
        f"Batch evaluation: {m.posts} posts in {m.batches} batches (size {report.batch_size})",
        f"Contamination: {_pct(m.contamination_rate)} of posts "
        f"({m.canary_batches_contaminated} canary batches contaminated)",
        f"Omission vs solo: {_pct(m.omission_rate)} ({m.omitted_posts} posts)",
        f"Claims delta (batch - solo): {_delta(m.claims_delta)} per post",
        f"Cost per post: batch {_micros(m.batch_cost_per_post_usd_micros)}, "
        f"solo {_micros(m.solo_cost_per_post_usd_micros)}",
    ]
    if report.errors:
        lines.append(f"Errors: {len(report.errors)}")
    return "\n".join(lines)


def render_batch_report(report: BatchEvalReport) -> str:
    m = compute_batch_metrics(report)
    lines = [
        "# Batch evaluation (E2 micro-batches)",
        "",
        f"Set: `{report.set_path}`, batch size {report.batch_size}.",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Posts / batches | {m.posts} / {m.batches} |",
        f"| Contamination rate | {_pct(m.contamination_rate)} ({m.contaminated_posts} posts) |",
        f"| Canary batches contaminated | {m.canary_batches_contaminated} |",
        f"| Omission rate | {_pct(m.omission_rate)} ({m.omitted_posts} posts) |",
        f"| Claims delta | {_delta(m.claims_delta)} |",
        f"| Cost per post, batch | {_micros(m.batch_cost_per_post_usd_micros)} |",
        f"| Cost per post, solo | {_micros(m.solo_cost_per_post_usd_micros)} |",
        "",
        "## Omission by position",
        "",
        "| Position | Omission rate |",
        "|---|---|",
        *(f"| {pos} | {_pct(rate)} |" for pos, rate in m.omission_by_position.items()),
    ]
    if report.errors:
        lines += ["", "## Errors", "", *(f"- {label}: {reason}" for label, reason in report.errors)]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- running


def _created(case: EvalCase) -> datetime:
    return datetime(case.date.year, case.date.month, case.date.day, tzinfo=UTC)


def _text(case: EvalCase) -> str:
    return case.post_text or case.claim


async def _solo(llm: Any, case: EvalCase, today: date) -> tuple[ExtractOutput, int]:
    payload = inputs.extract_input(
        post_id=case.id, lang=case.lang, text=_text(case), created_at=_created(case), today=today
    )
    result = await llm.complete(
        LLMRequest(
            role=names.ROLE_EXTRACT,
            level=Level(LEVEL),
            prompt_id=names.prompt_id(names.ROLE_EXTRACT),
            input=payload,
            schema=ExtractOutput,
            max_output_tokens=_SOLO_MAX_TOKENS,
            budget_scope=names.level_scope(LEVEL),
        )
    )
    if not isinstance(result.output, ExtractOutput):
        raise TypeError("unexpected extraction output type")
    return result.output, result.cost_usd_micros


async def _batch(
    llm: Any, index: int, cases: Sequence[EvalCase], today: date, seed: int
) -> tuple[ExtractBatchOutput, str, int, int]:
    """Returns ``(output, canary token, cost, canary position)``."""
    entries = [
        inputs.extract_input(
            post_id=c.id, lang=c.lang, text=_text(c), created_at=_created(c), today=today
        )["post"]
        for c in cases
    ]
    text, token = dom.canary_post(seed + index)
    canary = inputs.extract_input(
        post_id=f"{dom.CANARY_PREFIX}{index}", lang="fr", text=text, created_at=None, today=today
    )["post"]
    position = random.Random(seed * 1009 + index).randrange(len(entries) + 1)  # noqa: S311
    entries.insert(position, canary)
    result = await llm.complete(
        LLMRequest(
            role=names.ROLE_EXTRACT_BATCH,
            level=Level(LEVEL),
            prompt_id=names.prompt_id(names.ROLE_EXTRACT_BATCH),
            input=inputs.extract_batch_input(posts=entries, today=today),
            schema=ExtractBatchOutput,
            max_output_tokens=min(_MAX_OUTPUT_TOKENS, 400 + _PER_POST_OUTPUT_TOKENS * len(entries)),
            budget_scope=names.level_scope(LEVEL),
        )
    )
    if not isinstance(result.output, ExtractBatchOutput):
        raise TypeError("unexpected batch extraction output type")
    return result.output, token, result.cost_usd_micros, position


async def _compose(
    embedder: Any, cases: Sequence[EvalCase], settings: Settings, size: int
) -> list[list[int]]:
    result = await embedder.embed([_text(c) for c in cases], level=LEVEL, ref="eval:batch")
    candidates = [
        dom.BatchCandidate(
            content_id=i,
            tokens=dom.estimate_tokens(_text(c)),
            vector=result.vectors[i],
            solo=dom.has_injection_marker(_text(c)),
        )
        for i, c in enumerate(cases)
    ]
    return dom.compose(
        candidates,
        size=size,
        max_tokens=settings.fc_batch_max_tokens,
        dissimilarity=settings.fc_batch_dissimilarity,
    )


async def run_batch_eval(
    llm: Any,
    embedder: Any,
    cases: Sequence[EvalCase],
    *,
    settings: Settings,
    batch_size: int = 10,
    set_path: str = "",
    today: date | None = None,
    seed: int = 0,
) -> BatchEvalReport:
    """Compose, run the batches and the solo references; never raises for a model failure
    (it is listed in ``errors``)."""
    today = today or datetime.now(UTC).date()
    report = BatchEvalReport(set_path=set_path, batch_size=batch_size)
    groups = await _compose(embedder, cases, settings, batch_size)
    gate = asyncio.Semaphore(_CONCURRENCY)

    async def solo(case: EvalCase) -> tuple[ExtractOutput, int] | None:
        async with gate:
            try:
                return await _solo(llm, case, today)
            except LLMRouterError as exc:
                report.errors.append((case.id, f"solo: {type(exc).__name__}"))
                return None

    solos: dict[str, ExtractOutput] = {}
    for case, outcome in zip(cases, await asyncio.gather(*(solo(c) for c in cases)), strict=True):
        if outcome is not None:
            solos[case.id] = outcome[0]
            report.solo_cost_usd_micros += outcome[1]

    async def one(index: int, members: list[EvalCase]) -> None:
        async with gate:
            try:
                output, token, cost, canary_at = await _batch(llm, index, members, today, seed)
            except LLMRouterError as exc:
                report.errors.append((f"batch {index}", type(exc).__name__))
                return
        by_id: dict[str, ExtractOutput] = {}
        for item in output.posts:
            by_id.setdefault(item.post_id, item)
        contaminated = dom.canary_contaminated(
            token, {pid: [claim_text(c) for c in o.claims] for pid, o in by_id.items()}
        )
        report.batches.append(
            BatchRun(
                index=index,
                case_ids=tuple(c.id for c in members),
                canary_token=token,
                canary_contaminated=contaminated,
                size_sent=len(members) + 1,
                cost_usd_micros=cost,
            )
        )
        for pos, case in enumerate(members):
            if case.id not in solos:
                continue  # no reference: not scored
            sent = pos if pos < canary_at else pos + 1
            entry = by_id.get(case.id)
            report.posts.append(
                PostRun(
                    case_id=case.id,
                    batch=index,
                    position=sent,
                    text=_text(case),
                    batch_claims=tuple(claim_text(c) for c in entry.claims) if entry else (),
                    solo_claims=tuple(claim_text(c) for c in solos[case.id].claims),
                    returned=entry is not None,
                )
            )

    await asyncio.gather(*(one(i, [cases[j] for j in g]) for i, g in enumerate(groups)))
    report.batches.sort(key=lambda b: b.index)
    report.posts.sort(key=lambda p: (p.batch, p.position))
    return report


async def run_batch_command(
    settings: Settings,
    *,
    set_path: str,
    limit: int | None,
    report: str | None,
    batch_size: int,
) -> int:
    """Body of ``tower factcheck eval --batch``; returns the exit code (2 when the model
    routes are not seeded)."""
    from sqlalchemy import text

    from tower.core.db.database import Database
    from tower.core.factcheck import runtime
    from tower.services.investigate.evaluation import load_cases

    cases = load_cases(set_path, limit=limit)
    db = Database(settings)
    try:
        async with db.session() as session:
            routes = (await session.execute(text("SELECT count(*) FROM llm_routes"))).scalar_one()
        if not routes:
            print("No model routes configured: run `tower llm seed` first.")
            return 2
        run = await run_batch_eval(
            runtime.get_llm(settings, db),
            runtime.get_embedder(settings, db),
            cases,
            settings=settings,
            batch_size=batch_size,
            set_path=set_path,
        )
    finally:
        await db.dispose()
    print(render_batch_summary(run))
    if report:
        await asyncio.to_thread(Path(report).write_text, render_batch_report(run), encoding="utf-8")
        print(f"Report written to {report}")
    return 0
