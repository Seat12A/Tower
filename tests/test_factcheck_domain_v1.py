"""Pure domain of fact-check V1: signatures, bands, reuse, expiry, vectors, matching."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from typing import Any

import pytest

from tower.core.domain.factcheck.bands import (
    band_label,
    band_of,
    interval_contains,
    is_outdated,
    tolerance,
)
from tower.core.domain.factcheck.expiry import (
    DEFAULT_TTL_DAYS,
    parse_ttl_overrides,
    ttl_days,
    verdict_expiry,
)
from tower.core.domain.factcheck.matching import Candidate, MatchDecision, plan_match
from tower.core.domain.factcheck.reuse import ReuseContext, can_reuse, inheritable_note
from tower.core.domain.factcheck.signatures import (
    NormalizedSignature,
    SignatureComparison,
    compare_signatures,
    normalize_signature,
)
from tower.core.domain.factcheck.values import (
    Band,
    BandSide,
    ClaimType,
    MatchOutcome,
    ReuseRule,
)
from tower.core.domain.factcheck.values import (
    SignatureDifference as Diff,
)
from tower.core.domain.factcheck.vectors import cosine, medoid

POST = date(2026, 11, 1)


def sig(**over: Any) -> NormalizedSignature:
    base: dict[str, Any] = {
        "claim_type": "statistic",
        "subject": "Unemployment rate",
        "subject_qid": None,
        "predicate": "Equals",
        "value_low": 7,
        "value_high": 7,
        "unit": "%",
        "unit_scale": None,
        "currency": None,
        "value_date": None,
        "period_start": date(2024, 1, 1),
        "period_end": date(2024, 12, 31),
        "place": "FR",
        "primary_jurisdiction": None,
        "polarity": "affirmative",
        "definition_variant": "ilo",
        "comparison_base": None,
        "post_date": POST,
    }
    base.update(over)
    return normalize_signature(**base)


def cmp(a: NormalizedSignature, b: NormalizedSignature, days: int = 30) -> SignatureComparison:
    return compare_signatures(a, b, current_period_days=days)


# ---------------------------------------------------------------- signatures


def test_identical_signatures() -> None:
    c = cmp(sig(), sig())
    assert c.identical and not c.differences and not c.conflicting


@pytest.mark.parametrize(
    ("over", "diff"),
    [
        ({"value_low": 9, "value_high": 9}, Diff.VALUE),
        ({"polarity": "negative"}, Diff.POLARITY),
        ({"period_start": date(2023, 1, 1), "period_end": date(2023, 12, 31)}, Diff.PERIOD),
        ({"place": "FR-971"}, Diff.TERRITORY),
        ({"place": "DE"}, Diff.TERRITORY),
        ({"definition_variant": "cat_a"}, Diff.DEFINITION),
        ({"unit": "persons"}, Diff.UNIT),
        ({"unit_scale": "million"}, Diff.VALUE),
        ({"claim_type": "legal_text"}, Diff.CLAIM_TYPE),
        ({"subject": "Inflation rate"}, Diff.SUBJECT),
        ({"comparison_base": "year_on_year", "unit": "%"}, Diff.MISSING),
    ],
)
def test_trap_pairs_are_never_identical(over: dict[str, Any], diff: Diff) -> None:
    c = cmp(sig(), sig(**over))
    assert not c.identical
    assert diff in c.differences
    assert c.conflicting is (diff is not Diff.MISSING)


def test_scale_million_vs_thousand() -> None:
    a = sig(value_low=3, value_high=3, unit_scale="million")
    b = sig(value_low=3, value_high=3, unit_scale="thousand")
    assert a.value_low == D(3_000_000)
    assert b.value_low == D(3000)
    assert cmp(a, b).conflicting


def test_scale_equivalence_and_unknown_scale() -> None:
    a = sig(value_low=3, value_high=3, unit_scale="million")
    b = sig(value_low=3000, value_high=3000, unit_scale="thousand")
    assert cmp(a, b).identical
    weird = sig(value_low=3, value_high=3, unit_scale="myriad")
    assert weird.unit == "percent*myriad"
    assert Diff.UNIT in cmp(weird, sig(value_low=3, value_high=3)).differences


def test_currency() -> None:
    a = sig(unit="eur", currency="eur")
    b = sig(unit="eur", currency="usd")
    assert Diff.CURRENCY in cmp(a, b).differences
    assert cmp(a, b).conflicting
    only = cmp(a, sig(unit="eur", currency=None))
    assert only.differences == {Diff.MISSING} and not only.conflicting


def test_unit_aliases() -> None:
    for alias in ("%", "pct", "Percent", "pourcent"):
        assert sig(unit=alias).unit == "percent"
    for alias in ("people", "personnes", "Persons"):
        assert sig(unit=alias).unit == "person"
    assert sig(unit="€").unit == "EUR"
    assert sig(unit="$").unit == "USD"
    assert sig(unit=None).unit is None


def test_comparison_base_conflict_and_missing() -> None:
    a = sig(comparison_base="year_on_year")
    b = sig(comparison_base="quarter_on_quarter")
    assert Diff.COMPARISON_BASE in cmp(a, b).differences
    assert cmp(a, sig()).differences == {Diff.MISSING}


def test_open_vs_closed_interval() -> None:
    more_than = sig(value_low=10000, value_high=None)
    exact = sig(value_low=10000, value_high=10000)
    c = cmp(more_than, exact)
    assert Diff.VALUE in c.differences and c.conflicting
    assert cmp(more_than, sig(value_low=10000, value_high=None)).identical


def test_float_values_use_decimal() -> None:
    assert cmp(
        sig(value_low=0.1, value_high=0.1), sig(value_low=D("0.1"), value_high=D("0.1"))
    ).identical


def test_current_periods() -> None:
    a = sig(period_start=None, period_end=None, post_date=date(2026, 11, 1))
    near = sig(period_start=None, period_end=None, post_date=date(2026, 11, 11))
    far = sig(period_start=None, period_end=None, post_date=date(2026, 12, 31))
    assert a.period.current
    assert cmp(a, near).identical
    c = cmp(a, far)
    assert Diff.PERIOD in c.differences and c.conflicting


def test_current_vs_explicit_period() -> None:
    cur = sig(period_start=None, period_end=None, post_date=date(2026, 6, 1))
    containing = sig(period_start=date(2026, 1, 1), period_end=date(2026, 12, 31))
    outside = sig(period_start=date(2024, 1, 1), period_end=date(2024, 12, 31))
    c = cmp(cur, containing)
    assert c.differences == {Diff.MISSING} and not c.conflicting
    assert Diff.PERIOD in cmp(cur, outside).differences
    assert cmp(containing, cur).differences == {Diff.MISSING}


def test_unknown_current_periods_are_missing() -> None:
    a = sig(period_start=None, period_end=None, post_date=None)
    b = sig(period_start=None, period_end=None, post_date=date(2026, 1, 1))
    assert cmp(a, b).differences == {Diff.MISSING}
    assert cmp(a, a).differences == {Diff.MISSING}


def test_value_date_is_a_one_day_period() -> None:
    a = sig(period_start=None, period_end=None, value_date=date(2024, 5, 1))
    assert not a.period.current
    assert a.period.start == a.period.end == date(2024, 5, 1)
    assert cmp(a, sig(period_start=None, period_end=None, value_date=date(2024, 5, 1))).identical
    assert (
        Diff.PERIOD
        in cmp(a, sig(period_start=None, period_end=None, value_date=date(2024, 5, 2))).differences
    )


def test_one_sided_period() -> None:
    a = sig(period_start=date(2024, 1, 1), period_end=None)
    assert a.period.start == a.period.end == date(2024, 1, 1)
    b = sig(period_start=None, period_end=date(2024, 1, 1))
    assert cmp(a, b).identical


def test_subject_normalization() -> None:
    a = sig(subject="  Taux de  Chômage! ")
    b = sig(subject="taux de chomage")
    assert a.subject_key == "taux de chomage"
    assert cmp(a, b).identical
    assert sig(subject="   ").subject_key is None
    assert sig(subject=None).subject_key is None


def test_subject_qid_wins() -> None:
    a = sig(subject="France", subject_qid="q142")
    b = sig(subject="République française", subject_qid="Q142")
    assert a.subject_key == "Q142"
    assert cmp(a, b).identical
    # a plain-text subject never equals a QID key (casefolded text vs uppercased id)
    assert Diff.SUBJECT in cmp(a, sig(subject="Q142")).differences
    assert Diff.SUBJECT in cmp(a, sig(subject_qid="Q90")).differences


def test_subject_missing_one_side() -> None:
    c = cmp(sig(subject=None), sig())
    assert c.differences == {Diff.MISSING}


def test_territory_falls_back_to_jurisdiction() -> None:
    a = sig(place=None, primary_jurisdiction="fr")
    assert a.territory == "FR"
    assert cmp(a, sig()).identical
    assert cmp(sig(place=None), sig()).differences == {Diff.MISSING}


def test_predicate_is_not_identity() -> None:
    assert cmp(sig(predicate="equals"), sig(predicate="is_about")).identical
    assert sig(predicate=" Equals ").predicate == "equals"


# --------------------------------------------------------------------- bands

TAU = D("0.2")


def test_tolerance_floors() -> None:
    kw = {"ratio": D("0.05"), "floor_rate_points": D("0.2"), "floor_default": D(5)}
    assert tolerance(D(7), unit="percent", **kw) == D("0.35")
    assert tolerance(D("1.0"), unit="percent", **kw) == D("0.2")
    assert tolerance(D(1000), unit="person", **kw) == D(50)
    assert tolerance(D(10), unit=None, **kw) == D(5)
    assert tolerance(D(-1000), unit="person", **kw) == D(50)


def test_band_contains() -> None:
    assert band_of(D("6.5"), D("7.5"), D("7.4"), TAU) == (Band.CONTAINS, BandSide.NONE)
    assert band_of(D("6.5"), D("7.5"), D("6.5"), TAU) == (Band.CONTAINS, BandSide.NONE)


def test_band_upper_bound_excluded() -> None:
    assert band_of(D("6.5"), D("7.5"), D("7.5"), TAU) == (Band.WITHIN_1_TAU, BandSide.BELOW)


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        (D("6.4"), (Band.WITHIN_1_TAU, BandSide.ABOVE)),
        (D("6.3"), (Band.WITHIN_1_TAU, BandSide.ABOVE)),
        (D("6.0"), (Band.WITHIN_3_TAU, BandSide.ABOVE)),
        (D("5.9"), (Band.WITHIN_3_TAU, BandSide.ABOVE)),
        (D("5.8"), (Band.BEYOND, BandSide.ABOVE)),
        (D("7.7"), (Band.WITHIN_1_TAU, BandSide.BELOW)),
        (D("8.1"), (Band.WITHIN_3_TAU, BandSide.BELOW)),
        (D("8.2"), (Band.BEYOND, BandSide.BELOW)),
    ],
)
def test_band_distances(ref: D, expected: tuple[Band, BandSide]) -> None:
    assert band_of(D("6.5"), D("7.5"), ref, TAU) == expected


def test_band_open_intervals() -> None:
    assert band_of(D(9), None, D(10), TAU) == (Band.CONTAINS, BandSide.NONE)
    assert band_of(D(9), None, D(9), TAU) == (Band.CONTAINS, BandSide.NONE)
    assert band_of(D(9), None, D("8.9"), TAU) == (Band.WITHIN_1_TAU, BandSide.ABOVE)
    assert band_of(D(9), None, D(5), TAU) == (Band.BEYOND, BandSide.ABOVE)
    assert band_of(None, D(5), D(4), TAU) == (Band.CONTAINS, BandSide.NONE)
    assert band_of(None, D(5), D(5), TAU) == (Band.WITHIN_1_TAU, BandSide.BELOW)
    assert band_of(None, D(5), D(9), TAU) == (Band.BEYOND, BandSide.BELOW)
    with pytest.raises(ValueError, match="bound"):
        band_of(None, None, D(1), TAU)


def test_band_degenerate_interval() -> None:
    assert band_of(D(7), D(7), D(7), TAU) == (Band.CONTAINS, BandSide.NONE)
    assert band_of(D(9), D(9), D(7), TAU) == (Band.BEYOND, BandSide.ABOVE)
    assert band_of(D(7), D(7), D("7.1"), TAU) == (Band.WITHIN_1_TAU, BandSide.BELOW)
    assert not interval_contains(D(7), D(7), D("7.1"))


def test_band_labels() -> None:
    assert band_label(Band.CONTAINS, BandSide.NONE) == "contains"
    assert band_label(Band.WITHIN_1_TAU, BandSide.ABOVE) == "above_within_1_tau"
    assert band_label(Band.BEYOND, BandSide.BELOW) == "below_beyond"


def test_outdated() -> None:
    # claim "7 %" ([6.5, 7.5[): previous fact 7.2 inside, current 8.0 outside
    assert is_outdated(D("6.5"), D("7.5"), current=D(8), previous=D("7.2"), tau=TAU)
    # still matches the current value: not outdated
    assert not is_outdated(D("6.5"), D("7.5"), current=D("7.4"), previous=D("7.2"), tau=TAU)
    # matches neither
    assert not is_outdated(D("6.5"), D("7.5"), current=D(9), previous=D(10), tau=TAU)
    # no previous
    assert not is_outdated(D("6.5"), D("7.5"), current=D(9), previous=None, tau=TAU)
    # open interval
    assert is_outdated(D(9), None, current=D(5), previous=D(10), tau=TAU)
    assert not is_outdated(None, D(5), current=D(1), previous=D(2), tau=TAU)


# --------------------------------------------------------------------- reuse

NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)


def ctx(**over: Any) -> ReuseContext:
    base: dict[str, Any] = {
        "level": "N3",
        "direct_reuse_allowed": True,
        "verdict_expires_at": NOW + timedelta(days=1),
        "verdict_status": "active",
        "note_regime": "object",
        "note_expires_at": None,
        "now": NOW,
    }
    base.update(over)
    return ReuseContext(**base)


@pytest.mark.parametrize("rule", list(ReuseRule))
def test_can_reuse_nominal(rule: ReuseRule) -> None:
    assert can_reuse(ctx(), rule)
    assert can_reuse(ctx(verdict_expires_at=None), rule)


@pytest.mark.parametrize("rule", list(ReuseRule))
@pytest.mark.parametrize(
    "over",
    [
        {"level": "N4"},
        {"direct_reuse_allowed": False},
        {"verdict_status": "draft"},
        {"verdict_status": "superseded"},
        {"verdict_status": None},
        {"verdict_expires_at": NOW},
        {"verdict_expires_at": NOW - timedelta(seconds=1)},
    ],
)
def test_can_reuse_refused(rule: ReuseRule, over: dict[str, Any]) -> None:
    assert not can_reuse(ctx(**over), rule)


def test_inheritable_note() -> None:
    ident, band = ReuseRule.IDENTICAL_SIGNATURE, ReuseRule.BAND
    assert inheritable_note(ctx(note_regime="object"), ident)
    assert inheritable_note(ctx(note_regime="object"), band)
    assert inheritable_note(ctx(note_regime="formulation"), ident)
    assert not inheritable_note(ctx(note_regime="formulation"), band)
    assert not inheritable_note(ctx(note_regime="provisional"), ident)
    assert not inheritable_note(ctx(note_regime="provisional"), band)
    assert not inheritable_note(ctx(note_regime=None), ident)
    assert not inheritable_note(ctx(note_expires_at=NOW), ident)
    assert inheritable_note(ctx(note_expires_at=NOW + timedelta(hours=1)), band)


# -------------------------------------------------------------------- expiry


def test_default_ttls() -> None:
    expected = {
        ClaimType.STATISTIC: 120,
        ClaimType.HISTORICAL_FACT: 3650,
        ClaimType.QUOTE: 3650,
        ClaimType.LEGAL_TEXT: 180,
        ClaimType.ONGOING_SITUATION: 2,
        ClaimType.PROCEDURE: 14,
        ClaimType.RECORD: 365,
        ClaimType.CONDUCT: 365,
        ClaimType.ACCUSATION: 90,
        ClaimType.PREDICTION: 30,
        ClaimType.OPINION: 30,
        ClaimType.CAUSAL: 365,
        ClaimType.OTHER: 180,
    }
    assert dict(DEFAULT_TTL_DAYS) == expected
    assert set(DEFAULT_TTL_DAYS) == set(ClaimType)
    for claim_type, days in expected.items():
        assert ttl_days(claim_type) == days
        assert verdict_expiry(validated_at=NOW, claim_type=claim_type) == NOW + timedelta(days=days)


def test_overrides() -> None:
    assert ttl_days(ClaimType.STATISTIC, {"statistic": 10}) == 10
    assert ttl_days(ClaimType.QUOTE, {"statistic": 10}) == 3650
    assert verdict_expiry(
        validated_at=NOW, claim_type=ClaimType.STATISTIC, overrides={"statistic": 10}
    ) == NOW + timedelta(days=10)


def test_next_publication_caps() -> None:
    expiry = verdict_expiry(
        validated_at=NOW, claim_type=ClaimType.STATISTIC, next_publication=date(2026, 12, 1)
    )
    assert expiry == datetime(2026, 12, 1, tzinfo=UTC)
    far = verdict_expiry(
        validated_at=NOW, claim_type=ClaimType.ONGOING_SITUATION, next_publication=date(2027, 1, 1)
    )
    assert far == NOW + timedelta(days=2)


def test_provisional_cap() -> None:
    expiry = verdict_expiry(
        validated_at=NOW, claim_type=ClaimType.STATISTIC, has_provisional_note=True
    )
    assert expiry == NOW + timedelta(hours=6)
    both = verdict_expiry(
        validated_at=NOW,
        claim_type=ClaimType.STATISTIC,
        has_provisional_note=True,
        next_publication=date(2026, 11, 11),
    )
    assert both == datetime(2026, 11, 11, tzinfo=UTC)


def test_parse_overrides() -> None:
    assert parse_ttl_overrides("") == {}
    assert parse_ttl_overrides("{}") == {}
    assert parse_ttl_overrides('{"statistic": 30, "quote": 1}') == {"statistic": 30, "quote": 1}


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[1]",
        '{"statistic": -5}',
        '{"statistic": 0}',
        '{"statistic": "30"}',
        '{"statistic": 1.5}',
        '{"statistic": true}',
        '{"bogus": 3}',
    ],
)
def test_parse_overrides_invalid(raw: str) -> None:
    with pytest.raises(ValueError, match=r"\w"):
        parse_ttl_overrides(raw)


# ------------------------------------------------------------------- vectors


def test_cosine() -> None:
    assert cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine([1, 0], [-1, 0]) == pytest.approx(-1.0)
    assert cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine([0, 0], [1, 0]) == 0.0
    assert cosine([1, 0], [0, 0]) == 0.0
    assert cosine([2, 0], [5, 0]) == pytest.approx(1.0)
    with pytest.raises(ValueError, match=r"\w"):
        cosine([1], [1, 2])


def test_medoid() -> None:
    assert medoid([[1.0, 0.0]]) == 0
    assert medoid([[1, 0], [0.9, 0.1], [0, 1]]) == 1
    assert medoid([[1, 0], [1, 0], [1, 0]]) == 0  # tie: lowest index
    assert medoid([[0, 1], [1, 0]]) == 0  # symmetric tie
    assert medoid([[0, 1], [1, 0], [1, 0.01]]) == 2
    with pytest.raises(ValueError, match=r"\w"):
        medoid([])


# ------------------------------------------------------------------ matching

SAME, GREY = 0.9, 0.75
IDENT = SignatureComparison(True, frozenset())
MISSING = SignatureComparison(False, frozenset({Diff.MISSING}))


def cand(
    gid: int,
    *,
    lang: str = "fr",
    pivot: float = 0.95,
    orig: float | None = 0.95,
    comparison: SignatureComparison = IDENT,
    verdict: bool = False,
) -> Candidate:
    return Candidate(gid, lang, pivot, orig, comparison, verdict)


def plan(lang: str, *cands: Candidate) -> Any:
    return plan_match(lang, cands, same_threshold=SAME, grey_threshold=GREY)


def test_plan_empty() -> None:
    p = plan("fr")
    assert p.join_without_arbiter is None and not p.arbiter_queue and not p.rejected


def test_plan_join_without_arbiter() -> None:
    p = plan("fr", cand(1, orig=0.92), cand(2, orig=0.97))
    assert p.join_without_arbiter == 2  # best similarity first
    assert p.arbiter_queue == ()
    assert p.rejected[1] == "join_taken"


def test_plan_join_threshold_inclusive() -> None:
    assert plan("fr", cand(1, orig=SAME)).join_without_arbiter == 1


def test_plan_interlingual_always_arbiter() -> None:
    p = plan("fr", cand(1, lang="en", pivot=0.99, orig=None))
    assert p.join_without_arbiter is None
    assert p.arbiter_queue == (1,)
    assert plan("fr", cand(1, lang="en", pivot=0.5, orig=None)).rejected == {1: "below_grey"}


def test_plan_grey_zone() -> None:
    p = plan("fr", cand(1, orig=0.8))
    assert p.arbiter_queue == (1,) and p.join_without_arbiter is None
    assert plan("fr", cand(1, orig=GREY)).arbiter_queue == (1,)


def test_plan_below_grey() -> None:
    p = plan("fr", cand(1, orig=0.7, pivot=0.99))
    assert p.rejected == {1: "below_grey"} and not p.arbiter_queue


def test_plan_same_language_without_original_never_joins() -> None:
    p = plan("fr", cand(1, pivot=0.99, orig=None))
    assert p.join_without_arbiter is None and p.arbiter_queue == (1,)


def test_plan_uses_original_for_same_language() -> None:
    # pivot says near-identical but originals are far: not joined, below grey
    p = plan("fr", cand(1, pivot=0.99, orig=0.5))
    assert p.rejected == {1: "below_grey"}


def test_plan_conflict_rejection_strings() -> None:
    conflict = SignatureComparison(False, frozenset({Diff.VALUE, Diff.PERIOD}))
    p = plan(
        "fr",
        cand(1, comparison=conflict),
        cand(2, comparison=SignatureComparison(False, frozenset({Diff.POLARITY}))),
    )
    assert p.rejected == {1: "signature:period,value", 2: "signature:polarity"}
    assert p.join_without_arbiter is None and not p.arbiter_queue


def test_plan_conflict_beats_similarity() -> None:
    conflict = SignatureComparison(False, frozenset({Diff.VALUE}))
    p = plan("fr", cand(1, orig=1.0, comparison=conflict))
    assert p.join_without_arbiter is None


def test_plan_related_candidate() -> None:
    p = plan("fr", cand(1, orig=0.99, comparison=MISSING), cand(2, orig=0.95, comparison=MISSING))
    assert p.join_without_arbiter is None
    assert p.related_candidates == (1,)
    assert p.rejected[2] == "related_cap"
    assert not p.arbiter_queue
    low = plan("fr", cand(1, orig=0.5, comparison=MISSING))
    assert low.related_candidates == () and low.rejected == {1: "below_grey"}


def test_plan_arbiter_cap() -> None:
    p = plan("fr", cand(1, orig=0.85), cand(2, orig=0.84), cand(3, orig=0.83))
    assert p.arbiter_queue == (1, 2)
    assert p.rejected[3] == "arbiter_cap"


def test_plan_order_and_tie_break() -> None:
    p = plan("fr", cand(5, orig=0.8), cand(3, orig=0.8, verdict=True), cand(4, orig=0.8))
    assert p.arbiter_queue == (3, 4)  # verdict first, then lowest id


def test_plan_mixed() -> None:
    conflict = SignatureComparison(False, frozenset({Diff.TERRITORY}))
    p = plan(
        "fr",
        cand(1, orig=0.99, comparison=conflict),
        cand(2, lang="en", pivot=0.9, orig=None),
        cand(3, orig=0.8),
        cand(4, orig=0.85, comparison=MISSING),
    )
    assert p.arbiter_queue == (2, 3) or p.arbiter_queue == (3, 2)
    assert p.related_candidates == (4,)
    assert p.rejected == {1: "signature:territory"}


def test_match_decision_constructors() -> None:
    new = MatchDecision.new()
    assert new.outcome is MatchOutcome.NEW and new.group_id is None
    assert new.rule == "new:no_candidate" and not new.outdated
    joined = MatchDecision.join(
        7, "identical_signature:similarity", similarity=0.95, embedding=(0.1, 0.2)
    )
    assert joined.outcome is MatchOutcome.JOIN and joined.group_id == 7
    assert joined.embedding == (0.1, 0.2) and joined.external_factchecks == ()
    related = MatchDecision.new("new:related", related_group_id=3)
    assert related.related_group_id == 3
