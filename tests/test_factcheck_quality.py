"""Lot 6 quality tooling: confidence recalibration and E1 distillation measurement."""

from __future__ import annotations

import random
from dataclasses import fields
from typing import Any

import pytest
from sqlalchemy import text

from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain.factcheck import calibration as cal
from tower.core.domain.factcheck.confidence import (
    Coefficients,
    ConfidenceSignals,
    compute_confidence,
)
from tower.core.domain.factcheck.router import distillation_report
from tower.core.domain.factcheck.values import Agreement
from tower.core.factcheck import runtime
from tower.core.factcheck.calibration import (
    EXIT_NOT_ENOUGH_DATA,
    build_candidate,
    load_calibration_samples,
    run_activate_calibration,
    run_calibrate,
)
from tower.core.models.atlas import ConfidenceModel

from .test_factcheck_assembly import run_assemble, seed, verdict_row
from .test_factcheck_service import POST, FakePlatform, make_content, platform_item

# ----------------------------------------------------------------------- domain


@pytest.mark.parametrize(
    "signals",
    [
        ConfidenceSignals(),
        ConfidenceSignals(primary_direct=True, independent_origins=2),
        ConfidenceSignals(independent_origins=4, agreement=Agreement.SUBSTANTIVE),
        ConfidenceSignals(agreement=Agreement.STYLE, second_pass=False, causation=True),
        ConfidenceSignals(conflicting_sources=True, coverage_b=True, outcome_misleading=True),
    ],
)
def test_indicator_features_reproduce_the_linear_score(signals: ConfidenceSignals) -> None:
    features = cal.indicator_features(signals)
    assert set(features) == {f.name for f in fields(Coefficients)} - {"intercept"}
    assert cal.predict(Coefficients(), features, cap=0.9) == pytest.approx(
        compute_confidence(signals), abs=1e-4
    )


def test_eligibility_thresholds() -> None:
    assert cal.eligible_kind(199, 150) is None
    assert cal.eligible_kind(200, 99) == "reduced"
    assert cal.eligible_kind(250, 100) == "full"


def _synthetic(n: int, *, refusal_rate: float, seed: int = 3) -> list[cal.CalibrationSample]:
    """Kept outcomes depend mostly on a primary source and on the second opinion."""
    rng = random.Random(seed)  # noqa: S311
    out = []
    for _ in range(n):
        signals = ConfidenceSignals(
            primary_direct=rng.random() < 0.5,
            independent_origins=rng.choice((1, 2, 3)),
            agreement=rng.choice((Agreement.IDENTICAL, Agreement.STYLE, Agreement.SUBSTANTIVE)),
        )
        p = 0.15 + 0.5 * signals.primary_direct + 0.3 * (signals.agreement is Agreement.IDENTICAL)
        kept = rng.random() < min(p, 0.95)
        refusal = not kept and rng.random() < refusal_rate
        out.append(cal.CalibrationSample(cal.indicator_features(signals), int(kept), refusal))
    return out


def test_reduced_model_refits_only_the_intercept_and_four_coefficients() -> None:
    samples = _synthetic(300, refusal_rate=0.2)
    built = build_candidate(samples, Coefficients())
    assert built is not None
    kind, coefficients, metrics = built
    assert kind == "reduced" and metrics["fitted"] == ["intercept", *cal.REDUCED_FEATURES]
    assert coefficients.causation == Coefficients().causation  # untouched
    assert coefficients.intercept != Coefficients().intercept
    assert metrics["train_size"] + metrics["holdout_size"] == 300
    assert 0 <= metrics["ece"] <= 1 and metrics["ece_current"] is not None
    assert metrics["better_than_current"] == (metrics["ece"] < metrics["ece_current"])


def test_full_model_needs_one_hundred_refusals_and_shrinks_towards_the_prior() -> None:
    samples = _synthetic(400, refusal_rate=1.0)
    built = build_candidate(samples, Coefficients())
    assert built is not None and built[0] == "full"
    fitted = set(built[2]["fitted"])
    assert {"causation", "coverage_b"} <= fitted
    # a coefficient never observed (no signal) stays at its prior value
    assert built[1].coverage_b == pytest.approx(Coefficients().coverage_b, abs=1e-6)


def test_below_the_thresholds_nothing_is_built() -> None:
    assert build_candidate(_synthetic(150, refusal_rate=1.0), Coefficients()) is None


def test_expected_calibration_error() -> None:
    assert cal.expected_calibration_error([]) is None
    assert cal.expected_calibration_error([(0.9, 1)] * 9 + [(0.9, 0)]) == 0.0
    assert cal.expected_calibration_error([(0.9, 0)] * 10) == 0.9


def test_distillation_report_criterion() -> None:
    # 1-D vectors: positives score high, negatives low → half the calls avoided, no loss
    samples: list[tuple[list[float], int]] = [([1.0], 1)] * 50 + [([-1.0], 0)] * 50
    report = distillation_report([10.0], 0.0, samples, threshold=0.15)
    assert report["e2_call_reduction"] == 0.5 and report["recall"] == 1.0
    assert report["meets_criterion"] is True
    # a threshold that discards positives loses recall: criterion not met
    lossy = distillation_report([10.0], -10.0, samples, threshold=0.9)
    assert lossy["recall_loss"] > 0.02 and lossy["meets_criterion"] is False
    assert lossy["suggested_reduction"] == 0.5


# ----------------------------------------------------------------------- database


async def test_calibrate_refuses_without_enough_reviews(db: Database, settings: Settings) -> None:
    assert await run_calibrate(settings) == EXIT_NOT_ENOUGH_DATA
    async with db.session() as s:
        assert await s.scalar(text("SELECT count(*) FROM confidence_models")) == 0


async def test_review_labels_and_proposed_outcome(db: Database) -> None:
    kept = await make_content(db, "5001", POST)
    changed = await make_content(db, "5002", POST)
    rejected = await make_content(db, "5003", POST)
    ids = []
    for cid in (kept, changed, rejected):
        inv = await seed(db, cid, cited=[("P1", "insee.fr|primary", "INSEE", 0)])
        ids.append(inv.id)
    reviews: list[tuple[int, str, str, dict[str, Any]]] = [
        (ids[0], "accept", "endorsed", {}),
        (ids[1], "edit", "endorsed", {"veracity": ["mostly_true", "false"]}),
        (ids[2], "reject", "rejected", {}),
    ]
    async with db.transaction() as s:
        for inv_id, decision, outcome, diff in reviews:
            await s.execute(
                text(
                    "INSERT INTO reviews (verdict_id, reviewer, decision, outcome, diff)"
                    " SELECT id, 'staff:1', :d, :o, CAST(:diff AS jsonb) FROM verdicts"
                    " WHERE investigation_id = :i"
                ),
                {"d": decision, "o": outcome, "diff": __import__("json").dumps(diff), "i": inv_id},
            )
    async with db.session() as s:
        samples = await load_calibration_samples(s)
    assert sorted((x.label, x.refusal) for x in samples) == [(0, False), (0, True), (1, False)]
    assert all(x.features["primary_direct"] == 0.0 for x in samples)  # no primary_passages


async def test_activation_needs_a_measured_improvement(db: Database, settings: Settings) -> None:
    async with db.transaction() as s:
        for better in (False, True):
            s.add(
                ConfidenceModel(
                    kind="reduced",
                    coefficients={"intercept": -0.5},
                    metrics={"better_than_current": better, "ece": 0.05, "ece_current": 0.12},
                    reviews=250,
                    refusals=20,
                )
            )
    assert await run_activate_calibration(settings, model_id=999, reason="x") == 1
    assert await run_activate_calibration(settings, model_id=1, reason="x") == 1
    assert await run_activate_calibration(settings, model_id=2, reason="measured") == 0
    async with db.session() as s:
        assert await s.scalar(text("SELECT id FROM confidence_models WHERE active")) == 2


async def test_assembly_uses_the_active_recalibrated_model(
    db: Database, settings: Settings
) -> None:
    runtime.clear_overrides()
    async with db.transaction() as s:
        s.add(
            ConfidenceModel(
                kind="reduced",
                coefficients={"intercept": -3.0},
                metrics={"better_than_current": True},
                reviews=250,
                refusals=0,
                active=True,
            )
        )
    cid = await make_content(db, "5010", POST)
    inv = await seed(db, cid, cited=[("P1", "blog.example|tertiary", None, 5)])
    runtime.set_override("platform:x", FakePlatform(platform_item("5010")))
    try:
        await run_assemble(db, settings, inv.id)
    finally:
        runtime.clear_overrides()
    confidence = (await verdict_row(db, inv.id))[0]
    expected = compute_confidence(
        ConfidenceSignals(independent_origins=1), Coefficients().with_overrides({"intercept": -3.0})
    )
    assert confidence == expected and confidence < 0.1
