"""Training of the E1 router (``tower factcheck train-router`` / ``activate-router``).

Offline, never in a production replica: the labels distil E2 (extraction outcome) and the
reviewers (``triage_false_positive`` / ``not_worth_checking`` override E2). Pure-Python
logistic regression over the stored vectors (see ``core/domain/factcheck/router.py``).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.config import Settings
from tower.core.db.vector import parse_vector
from tower.core.domain.errors import DomainError
from tower.core.domain.factcheck.router import (
    MIN_RECALL_TO_ACTIVATE,
    evaluate,
    stratified_split,
    train_logistic,
)
from tower.core.domain.security import Actor
from tower.core.factcheck import names
from tower.core.factcheck.profiles import active_profile
from tower.core.logging import get_logger
from tower.core.models.llm import LLMModel, LLMRoute
from tower.core.usecases.routing import activate_router_model, create_router_model

__all__ = [
    "LabelledSample",
    "TrainingResult",
    "embedding_model_in_use",
    "fit",
    "load_samples",
    "run_activate",
    "run_train",
]

log = get_logger(__name__)

CLI_ACTOR = "system:cli"
NEGATIVE_LABELS = ("triage_false_positive", "not_worth_checking")
EXIT_NOT_ENOUGH_SAMPLES = 2

_SAMPLES = text(
    """
    SELECT ce.content_id, CAST(ce.embedding AS TEXT) AS embedding, ce.lang,
           CASE WHEN EXISTS (
                SELECT 1 FROM watched_accounts wa
                WHERE wa.account_id = ci.author_account_id AND wa.active
           ) THEN 'N3' ELSE 'N1' END AS level,
           CASE
             WHEN EXISTS (
                SELECT 1 FROM content_claims cc
                JOIN claim_group_members m ON m.claim_id = cc.claim_id
                JOIN verdicts v ON v.claim_group_id = m.group_id
                JOIN reviews r ON r.verdict_id = v.id
                WHERE cc.content_id = ce.content_id AND r.labels && CAST(:negative AS TEXT[])
             ) THEN 0
             WHEN EXISTS (
                SELECT 1 FROM content_claims cc JOIN claims c ON c.id = cc.claim_id
                WHERE cc.content_id = ce.content_id AND c.verifiability >= 0.5
                  AND c.claim_type NOT IN ('opinion', 'prediction')
             ) THEN 1
             WHEN NOT EXISTS (
                SELECT 1 FROM content_claims cc WHERE cc.content_id = ce.content_id
             ) AND EXISTS (
                SELECT 1 FROM jobs j
                WHERE j.kind = :extract_kind AND j.status = 'succeeded'
                  AND j.dedupe_key LIKE 'ext:' || ce.content_id || ':%'
             ) THEN 0
           END AS label
    FROM content_embeddings ce
    JOIN content_items ci ON ci.id = ce.content_id
    WHERE ce.created_at >= :since AND ce.model = :model
    """
)


@dataclass(frozen=True, slots=True)
class LabelledSample:
    content_id: int
    vector: list[float]
    label: int
    lang: str
    level: str

    @property
    def stratum(self) -> tuple[str, str, int]:
        return (self.lang, self.level, self.label)


@dataclass(frozen=True, slots=True)
class TrainingResult:
    weights: list[float]
    bias: float
    metrics: dict[str, Any]
    sample_size: int


async def embedding_model_in_use(session: AsyncSession, settings: Settings) -> str:
    """Model name of the embeddings E1 produces now (the ``embed`` route's model)."""
    if settings.llm_fake:
        return "fake-embedding-512"
    name = await session.scalar(
        select(LLMModel.model)
        .join(LLMRoute, LLMRoute.model_id == LLMModel.id)
        .where(LLMRoute.role == names.ROLE_EMBED, LLMRoute.active.is_(True), LLMRoute.level == "*")
    )
    return name or "text-embedding-3-small"


async def load_samples(
    session: AsyncSession, *, since: datetime, model: str
) -> list[LabelledSample]:
    rows = (
        await session.execute(
            _SAMPLES,
            {
                "since": since,
                "model": model,
                "negative": list(NEGATIVE_LABELS),
                "extract_kind": names.JOB_EXTRACT,
            },
        )
    ).all()
    out: list[LabelledSample] = []
    for row in rows:
        if row.label is None:
            continue  # no label: skipped
        vector = parse_vector(row.embedding)
        if vector:
            out.append(LabelledSample(row.content_id, vector, int(row.label), row.lang, row.level))
    return out


def fit(
    samples: list[LabelledSample],
    *,
    threshold: float,
    holdout: float,
    per_stratum_cap: int | None,
    seed: int,
) -> TrainingResult:
    """Stratified split, training on the train part, evaluation on the held-out part."""
    train, held = stratified_split(
        samples,
        lambda s: s.stratum,
        holdout=holdout,
        per_stratum_cap=per_stratum_cap,
        seed=seed,
    )
    weights, bias = train_logistic([(s.vector, s.label) for s in train], seed=seed)
    metrics: dict[str, Any] = dict(
        evaluate(weights, bias, [(s.vector, s.label) for s in held], threshold=threshold)
    )
    counts = Counter(s.stratum for s in train + held)
    metrics.update(
        threshold=threshold,
        seed=seed,
        train_size=len(train),
        holdout_size=len(held),
        strata={f"{lang}|{level}|{label}": n for (lang, level, label), n in sorted(counts.items())},
    )
    return TrainingResult(weights, bias, metrics, len(train) + len(held))


def _summary(metrics: dict[str, Any]) -> str:
    return (
        f"recall={metrics['recall']:.3f} precision={metrics['precision']:.3f} "
        f"accuracy={metrics['accuracy']:.3f} auc={metrics['auc']:.3f} "
        f"holdout={metrics['holdout_size']} (pos={int(metrics['n_pos'])}, "
        f"neg={int(metrics['n_neg'])}) threshold={metrics['threshold']}"
    )


async def run_train(
    settings: Settings,
    *,
    days: int = 14,
    min_samples: int = 200,
    holdout: float = 0.2,
    per_stratum_cap: int | None = 2000,
    seed: int = 0,
    dry_run: bool = False,
) -> int:
    """CLI entry point. Returns the exit code (2: not enough labelled samples)."""
    from tower.core.db.database import Database

    db = Database(settings)
    try:
        since = datetime.now(UTC) - timedelta(days=days)
        async with db.session() as session:
            model = await embedding_model_in_use(session, settings)
            samples = await load_samples(session, since=since, model=model)
            threshold = (await active_profile(session, settings)).level("N1").e1_threshold
        if len(samples) < min_samples:
            print(
                f"Not enough labelled samples: {len(samples)} found, {min_samples} required "
                f"(last {days} days, model {model}). Nothing written."
            )
            return EXIT_NOT_ENOUGH_SAMPLES
        result = fit(
            samples,
            threshold=threshold,
            holdout=holdout,
            per_stratum_cap=per_stratum_cap,
            seed=seed,
        )
        print(f"Trained on {result.sample_size} labelled samples ({model}).")
        print(_summary(result.metrics))
        if dry_run:
            print("Dry run: no router model written.")
            return 0
        async with db.transaction() as session:
            row = await create_router_model(
                session,
                Actor.system(CLI_ACTOR),
                embedding_model=model,
                weights=result.weights,
                bias=result.bias,
                metrics=result.metrics,
                sample_size=result.sample_size,
            )
            version = row.version
        print(f"Router model version {version} stored (inactive).")
        return 0
    finally:
        await db.dispose()


async def run_activate(settings: Settings, *, version: int, force: bool, reason: str) -> int:
    """CLI entry point. Returns the exit code (1: refused)."""
    from tower.core.db.database import Database

    db = Database(settings)
    try:
        async with db.transaction() as session:
            model = await embedding_model_in_use(session, settings)
            try:
                row = await activate_router_model(
                    session,
                    Actor.system(CLI_ACTOR),
                    version=version,
                    expected_model=model,
                    force=force,
                    reason=reason,
                )
            except DomainError as exc:
                print(f"Refused: {exc} [{exc.code}]")
                return 1
            print(
                f"Router model version {row.version} is now active "
                f"(recall target {MIN_RECALL_TO_ACTIVATE})."
            )
        return 0
    finally:
        await db.dispose()
