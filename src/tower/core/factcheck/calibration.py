"""Confidence recalibration (``tower factcheck calibrate`` / ``activate-calibration``).

Offline, never in a production replica. Samples are the reviewers' decisions on drafts
(one per verdict, the latest): label 1 when the outcome was kept, 0 when the draft was
rejected or its outcome changed. Below the thresholds of
``core/domain/factcheck/calibration.py`` nothing is produced (exit code 2); a candidate is
stored inactive with its held-out measurement, and activation is refused unless its
expected calibration error beats the current coefficients on the same held-out set.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.config import Settings
from tower.core.domain.factcheck.calibration import (
    FULL_MIN_REFUSALS,
    REDUCED_MIN_REVIEWS,
    CalibrationSample,
    brier,
    eligible_kind,
    expected_calibration_error,
    fit_model,
    indicator_features,
    predict,
    split_holdout,
)
from tower.core.domain.factcheck.confidence import Coefficients
from tower.core.domain.factcheck.values import ReviewDecision, Veracity
from tower.core.factcheck import runtime
from tower.core.factcheck.signals import stored_signals
from tower.core.logging import get_logger
from tower.core.models.atlas import ConfidenceModel
from tower.core.models.factcheck import Investigation, Verdict

__all__ = [
    "EXIT_NOT_ENOUGH_DATA",
    "build_candidate",
    "load_calibration_samples",
    "run_activate_calibration",
    "run_calibrate",
]

log = get_logger(__name__)

CLI_ACTOR = "system:cli"
EXIT_NOT_ENOUGH_DATA = 2

_REVIEWS = text(
    """
    SELECT DISTINCT ON (r.verdict_id) r.verdict_id, r.decision, r.diff, c.claim_type
    FROM reviews r
    JOIN verdicts v ON v.id = r.verdict_id
    JOIN claim_groups g ON g.id = v.claim_group_id
    JOIN claims c ON c.id = g.representative_claim_id
    WHERE v.investigation_id IS NOT NULL
    ORDER BY r.verdict_id, r.created_at DESC, r.id DESC
    """
)


async def load_calibration_samples(session: AsyncSession) -> list[CalibrationSample]:
    """One sample per reviewed draft, with the signals of the outcome the model proposed."""
    samples: list[CalibrationSample] = []
    for row in (await session.execute(_REVIEWS)).all():
        verdict = await session.get(Verdict, row.verdict_id)
        if verdict is None or verdict.investigation_id is None:
            continue
        investigation = await session.get(Investigation, verdict.investigation_id)
        if investigation is None:
            continue
        diff = row.diff or {}
        changed = isinstance(diff.get("veracity"), list) and len(diff["veracity"]) == 2
        proposed = Veracity(diff["veracity"][0]) if changed else None
        signals = await stored_signals(
            session, verdict, investigation, str(row.claim_type), veracity=proposed
        )
        refusal = row.decision == ReviewDecision.REJECT.value
        samples.append(
            CalibrationSample(
                features=indicator_features(signals),
                label=0 if refusal or changed else 1,
                refusal=refusal,
            )
        )
    return samples


def build_candidate(
    samples: list[CalibrationSample],
    current: Coefficients,
    *,
    holdout: float = 0.25,
    seed: int = 0,
    cap: float = 1.0,
) -> tuple[str, Coefficients, dict[str, Any]] | None:
    """``(kind, coefficients, metrics)`` or ``None`` below the thresholds. The metrics
    compare the candidate and the current coefficients on the same held-out samples."""
    reviews = len(samples)
    refusals = sum(1 for s in samples if s.refusal)
    kind = eligible_kind(reviews, refusals)
    if kind is None:
        return None
    train, held = split_holdout(samples, holdout=holdout, seed=seed)
    fit = fit_model(train, current, kind=kind)
    candidate_pairs = [(predict(fit.coefficients, s.features, cap), s.label) for s in held]
    current_pairs = [(predict(current, s.features, cap), s.label) for s in held]
    metrics: dict[str, Any] = {
        "kind": kind,
        "reviews": reviews,
        "refusals": refusals,
        "train_size": len(train),
        "holdout_size": len(held),
        "seed": seed,
        "fitted": list(fit.fitted),
        "iterations": fit.iterations,
        "train_log_loss": fit.log_loss,
        "ece": expected_calibration_error(candidate_pairs),
        "ece_current": expected_calibration_error(current_pairs),
        "brier": brier(candidate_pairs),
        "brier_current": brier(current_pairs),
    }
    ece, ece_current = metrics["ece"], metrics["ece_current"]
    metrics["better_than_current"] = bool(
        ece is not None and ece_current is not None and ece < ece_current
    )
    metrics["meets_lot_criterion"] = bool(ece is not None and ece < 0.1)
    return kind, fit.coefficients, metrics


async def run_calibrate(
    settings: Settings, *, holdout: float = 0.25, seed: int = 0, dry_run: bool = False
) -> int:
    """CLI entry point. Returns the exit code (2: not enough reviewed data)."""
    from tower.core.db.database import Database

    db = Database(settings)
    try:
        async with db.session() as session:
            samples = await load_calibration_samples(session)
        current, thresholds = runtime.confidence_settings(settings)
        built = build_candidate(samples, current, holdout=holdout, seed=seed, cap=thresholds.cap)
        refusals = sum(1 for s in samples if s.refusal)
        if built is None:
            print(
                f"Not enough reviewed data: {len(samples)} reviews ({REDUCED_MIN_REVIEWS} "
                f"required for the reduced model), {refusals} refusals ({FULL_MIN_REFUSALS} "
                "required for the full model). Nothing produced. [TWR-FCK-046]"
            )
            return EXIT_NOT_ENOUGH_DATA
        kind, coefficients, metrics = built
        print(
            f"{kind} model fitted on {metrics['train_size']} reviews; held-out ECE "
            f"{metrics['ece']} (current {metrics['ece_current']}), Brier {metrics['brier']} "
            f"(current {metrics['brier_current']}); "
            f"{'better' if metrics['better_than_current'] else 'NOT better'} than current."
        )
        if dry_run:
            print("Dry run: no model written.")
            return 0
        async with db.transaction() as session:
            row = ConfidenceModel(
                kind=kind,
                coefficients={k: v for k, v in asdict(coefficients).items()},
                metrics=metrics,
                reviews=metrics["reviews"],
                refusals=metrics["refusals"],
            )
            session.add(row)
            await session.flush()
            model_id = row.id
        log.info("factcheck.calibration_stored", model=model_id, kind=kind, ece=metrics["ece"])
        print(f"Confidence model {model_id} stored (inactive).")
        return 0
    finally:
        await db.dispose()


async def run_activate_calibration(settings: Settings, *, model_id: int, reason: str) -> int:
    """CLI entry point (exit 1: refused). Activation needs a measured improvement: the
    candidate's held-out ECE must be lower than the current coefficients' one."""
    from tower.core.db.database import Database

    db = Database(settings)
    try:
        async with db.transaction() as session:
            row = await session.get(ConfidenceModel, model_id, with_for_update=True)
            if row is None:
                print(f"Refused: confidence model {model_id} not found [TWR-FCK-047]")
                return 1
            if not row.metrics.get("better_than_current"):
                print(
                    f"Refused: model {model_id} does not beat the current calibration "
                    f"(ECE {row.metrics.get('ece')} vs {row.metrics.get('ece_current')}) "
                    "[TWR-FCK-048]"
                )
                return 1
            await session.execute(
                update(ConfidenceModel).where(ConfidenceModel.active.is_(True)).values(active=False)
            )
            row.active = True
            row.activated_at = datetime.now(UTC)
            row.activated_by = CLI_ACTOR
            log.info("factcheck.calibration_activated", model=model_id, reason=reason[:200])
        print(f"Confidence model {model_id} is now active.")
        return 0
    finally:
        await db.dispose()


async def active_coefficients(session: AsyncSession) -> dict[str, float] | None:
    """Coefficients of the active recalibrated model, if any."""
    value = await session.scalar(
        select(ConfidenceModel.coefficients).where(ConfidenceModel.active.is_(True))
    )
    return dict(value) if value else None
