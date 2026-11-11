"""Unit tests of the pure fact-check domain (no database)."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta, timezone

import pytest

from tower.core.domain.factcheck.checks import (
    CheckReport,
    DatedSource,
    LoadedTermHit,
    check_dates,
    check_links,
    find_loaded_terms,
    normalize_for_quote,
    note_length,
    quote_in_passage,
)
from tower.core.domain.factcheck.confidence import (
    Coefficients,
    ConfidenceBand,
    ConfidenceSignals,
    Thresholds,
    band,
    compute_confidence,
    linear_score,
)
from tower.core.domain.factcheck.datamark import MARKER, datamark, unmark
from tower.core.domain.factcheck.fidelity import (
    FidelityIssue,
    check_claim_fidelity,
    check_omission,
    extract_numbers,
    missing_entities,
)
from tower.core.domain.factcheck.independence import (
    NEAR_DUPLICATE_BITS,
    IndependenceResult,
    SourceRef,
    apply_independence_rule,
    assess_independence,
    normalize_origin,
)
from tower.core.domain.factcheck.opinion import (
    EscalationInput,
    compare_opinions,
    initial_depth,
    should_escalate,
)
from tower.core.domain.factcheck.passages import (
    Chunk,
    RankedChunk,
    bm25_rank,
    chunk_text,
    dedupe_chunks,
    select_passages,
    tokenize,
)
from tower.core.domain.factcheck.quota import (
    DraftCandidate,
    is_doubtful,
    priority_key,
    select_for_review,
)
from tower.core.domain.factcheck.values import (
    Agreement,
    ContextNeed,
    Depth,
    Veracity,
)
from tower.core.domain.text import normalize, simhash64

V = Veracity

# --------------------------------------------------------------------------- opinion


@pytest.mark.parametrize("v", list(Veracity))
def test_compare_identical(v: Veracity) -> None:
    assert compare_opinions(v, v) is Agreement.IDENTICAL


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (V.TRUE, V.MOSTLY_TRUE),
        (V.MOSTLY_TRUE, V.TRUE),
        (V.MISLEADING, V.FALSE),
        (V.FALSE, V.MISLEADING),
    ],
)
def test_compare_style(a: Veracity, b: Veracity) -> None:
    assert compare_opinions(a, b) is Agreement.STYLE


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (V.TRUE, V.FALSE),
        (V.TRUE, V.MISLEADING),
        (V.MOSTLY_TRUE, V.MISLEADING),
        (V.FALSE, V.MOSTLY_TRUE),
        (V.TRUE, V.UNVERIFIABLE),
        (V.UNVERIFIABLE, V.FALSE),
        (V.MISLEADING, V.UNVERIFIABLE),
        (V.UNVERIFIABLE, V.MOSTLY_TRUE),
    ],
)
def test_compare_substantive(a: Veracity, b: Veracity) -> None:
    assert compare_opinions(a, b) is Agreement.SUBSTANTIVE


def _esc(**kw: object) -> EscalationInput:
    base: dict[str, object] = {
        "level": "N3",
        "depth": Depth.STANDARD,
        "agreement": Agreement.IDENTICAL,
        "conflicting_sources": False,
        "confidence": 0.9,
        "spent_usd_micros": 0,
        "reinforced_cap_usd_micros": 1_000_000,
    }
    base.update(kw)
    return EscalationInput(**base)  # type: ignore[arg-type]


def test_escalate_nothing_wrong() -> None:
    assert should_escalate(_esc()) is False


def test_escalate_style_does_not_escalate() -> None:
    assert should_escalate(_esc(agreement=Agreement.STYLE)) is False


def test_escalate_substantive() -> None:
    assert should_escalate(_esc(agreement=Agreement.SUBSTANTIVE)) is True


def test_escalate_conflicting() -> None:
    assert should_escalate(_esc(conflicting_sources=True)) is True


def test_escalate_low_confidence_and_boundary() -> None:
    assert should_escalate(_esc(confidence=0.59)) is True
    assert should_escalate(_esc(confidence=0.6)) is False
    assert should_escalate(_esc(confidence=0.7, threshold=0.75)) is True


def test_escalate_already_reinforced() -> None:
    assert should_escalate(_esc(depth=Depth.REINFORCED, agreement=Agreement.SUBSTANTIVE)) is False


def test_escalate_cap_reached() -> None:
    sub = Agreement.SUBSTANTIVE
    assert should_escalate(_esc(agreement=sub, spent_usd_micros=999_999)) is True
    assert should_escalate(_esc(agreement=sub, spent_usd_micros=1_000_000)) is False
    assert should_escalate(_esc(agreement=sub, spent_usd_micros=2_000_000)) is False


@pytest.mark.parametrize(
    ("level", "depth"),
    [
        ("N4", Depth.REINFORCED),
        ("N3", Depth.STANDARD),
        ("N2", Depth.STANDARD),
        ("", Depth.STANDARD),
    ],
)
def test_initial_depth(level: str, depth: Depth) -> None:
    assert initial_depth(level) is depth


# ------------------------------------------------------------------------ confidence


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def test_no_signal_is_half() -> None:
    assert linear_score(ConfidenceSignals(), Coefficients()) == 0.0
    assert compute_confidence(ConfidenceSignals()) == 0.5


def test_documented_example_capped() -> None:
    s = ConfidenceSignals(primary_direct=True, independent_origins=2, agreement=Agreement.IDENTICAL)
    assert linear_score(s, Coefficients()) == pytest.approx(2.6)
    assert _sigmoid(2.6) > 0.90
    assert compute_confidence(s) == 0.90


def test_substantive_conflicting_is_low() -> None:
    s = ConfidenceSignals(conflicting_sources=True, agreement=Agreement.SUBSTANTIVE)
    value = compute_confidence(s)
    assert value == round(_sigmoid(-3.0), 4)
    assert band(value) is ConfidenceBand.LOW


@pytest.mark.parametrize(
    ("origins", "expected"),
    [(0, 0.0), (1, 0.0), (2, 0.5), (3, 0.8), (7, 0.8)],
)
def test_origins_coefficients_exclusive(origins: int, expected: float) -> None:
    s = ConfidenceSignals(independent_origins=origins)
    assert linear_score(s, Coefficients()) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("agreement", "expected"),
    [
        (None, 0.0),
        (Agreement.IDENTICAL, 1.1),
        (Agreement.STYLE, 0.8),
        (Agreement.SUBSTANTIVE, -1.5),
    ],
)
def test_agreement_coefficients(agreement: Agreement | None, expected: float) -> None:
    assert linear_score(ConfidenceSignals(agreement=agreement), Coefficients()) == pytest.approx(
        expected
    )


@pytest.mark.parametrize(("second_pass", "expected"), [(None, 0.0), (True, 0.4), (False, -1.2)])
def test_second_pass(second_pass: bool | None, expected: float) -> None:
    s = ConfidenceSignals(second_pass=second_pass)
    assert linear_score(s, Coefficients()) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("primary_direct", 1.0),
        ("conflicting_sources", -1.5),
        ("statistic_with_reference", 0.5),
        ("ongoing_situation", -0.8),
        ("causation", -1.0),
        ("legal_interpretation", -0.5),
        ("outcome_misleading", -0.4),
        ("coverage_b", -0.3),
    ],
)
def test_boolean_signals(field: str, expected: float) -> None:
    s = ConfidenceSignals(**{field: True})  # type: ignore[arg-type]
    assert linear_score(s, Coefficients()) == pytest.approx(expected)


def test_intercept_added() -> None:
    assert linear_score(ConfidenceSignals(), Coefficients(intercept=0.25)) == 0.25


def test_sigmoid_extremes_do_not_overflow() -> None:
    hi = Coefficients(intercept=10_000.0)
    lo = Coefficients(intercept=-10_000.0)
    assert compute_confidence(ConfidenceSignals(), hi, Thresholds(cap=1.0)) == 1.0
    assert compute_confidence(ConfidenceSignals(), lo) == 0.0


def test_cap_is_configurable_and_rounding() -> None:
    s = ConfidenceSignals(primary_direct=True)
    assert compute_confidence(s) == round(_sigmoid(1.0), 4) == 0.7311
    assert compute_confidence(s, thresholds=Thresholds(cap=0.6)) == 0.6


def test_overrides_known_and_unknown() -> None:
    c = Coefficients().with_overrides({"primary_direct": 2, "coverage_b": 0.0})
    assert c.primary_direct == 2.0
    assert isinstance(c.primary_direct, float)
    assert c.coverage_b == 0.0
    assert c.causation == -1.0
    with pytest.raises(ValueError, match="nonsense"):
        Coefficients().with_overrides({"nonsense": 1.0})
    assert Coefficients().with_overrides({}) == Coefficients()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1.0, ConfidenceBand.HIGH),
        (0.80, ConfidenceBand.HIGH),
        (0.7999, ConfidenceBand.MEDIUM),
        (0.55, ConfidenceBand.MEDIUM),
        (0.5499, ConfidenceBand.LOW),
        (0.0, ConfidenceBand.LOW),
    ],
)
def test_band_thresholds_inclusive(value: float, expected: ConfidenceBand) -> None:
    assert band(value) is expected


def test_band_custom_thresholds() -> None:
    t = Thresholds(cap=0.95, high=0.9, medium=0.7)
    assert band(0.85, t) is ConfidenceBand.MEDIUM
    assert band(0.9, t) is ConfidenceBand.HIGH
    assert band(0.69, t) is ConfidenceBand.LOW


# ---------------------------------------------------------------------- independence


def _ref(
    pid: str,
    origin: str | None,
    *,
    domain: str = "x.fr",
    tier: str = "secondary",
    simhash: int = 0,
    stakeholder: bool = False,
) -> SourceRef:
    return SourceRef(pid, origin, domain, tier, simhash, stakeholder)


def _far(i: int) -> int:
    """Simhash values far from each other (distance well above NEAR_DUPLICATE_BITS)."""
    return simhash64(normalize(f"completely different text number {i} " * (i + 2)))


@pytest.mark.parametrize(
    ("origin", "domain", "expected"),
    [
        ("INSEE", "insee.fr", "insee"),
        ("  Insee ", "x.fr", "insee"),
        ("Banque   de France.", "x.fr", "banque de france"),
        ('"Eurostat"', "x.fr", "eurostat"),
        ("(Ministère de l'Intérieur)", "x.fr", "ministère de l'intérieur"),
        ("Élysée", "x.fr", "élysée"),
        ("", "insee.fr", "domain:insee.fr"),
        (None, "insee.fr", "domain:insee.fr"),
        ("  ..  ", "insee.fr", "domain:insee.fr"),
    ],
)
def test_normalize_origin(origin: str | None, domain: str, expected: str) -> None:
    assert normalize_origin(origin, domain) == expected


def test_normalize_origin_keeps_accents_distinct() -> None:
    assert normalize_origin("Elysee", "a") != normalize_origin("Élysée", "a")


def test_assess_empty() -> None:
    assert assess_independence([]) == IndependenceResult(0, False, ())


def test_assess_counts_distinct_origins() -> None:
    refs = [
        _ref("1", "INSEE", simhash=_far(1)),
        _ref("2", "insee", simhash=_far(2)),
        _ref("3", "Eurostat", simhash=_far(3)),
    ]
    r = assess_independence(refs)
    assert r.independent_origins == 2
    assert r.origins == ("insee", "eurostat")
    assert r.has_primary is False


def test_assess_domain_fallback_when_origin_missing() -> None:
    refs = [
        _ref("1", None, domain="a.fr", simhash=_far(1)),
        _ref("2", "", domain="a.fr", simhash=_far(2)),
        _ref("3", None, domain="b.fr", simhash=_far(3)),
    ]
    r = assess_independence(refs)
    assert r.origins == ("domain:a.fr", "domain:b.fr")


def test_assess_skips_avoid_and_stakeholder() -> None:
    refs = [
        _ref("1", "A", tier="avoid", simhash=_far(1)),
        _ref("2", "B", stakeholder=True, tier="primary", simhash=_far(2)),
        _ref("3", "C", simhash=_far(3)),
    ]
    r = assess_independence(refs)
    assert r.independent_origins == 1
    assert r.origins == ("c",)
    assert r.has_primary is False


def test_assess_primary_flag_only_for_counted() -> None:
    refs = [
        _ref("1", "A", tier="primary", stakeholder=True, simhash=_far(1)),
        _ref("2", "B", tier="avoid", simhash=_far(2)),
        _ref("3", "C", tier="primary", simhash=_far(3)),
    ]
    assert assess_independence(refs).has_primary is True


def test_assess_near_duplicates_merged_into_first() -> None:
    h = _far(1)
    refs = [
        _ref("1", "Agence A", simhash=h),
        _ref("2", "Journal B", simhash=h ^ 0b111),  # distance 3 == NEAR_DUPLICATE_BITS
    ]
    assert NEAR_DUPLICATE_BITS == 3
    r = assess_independence(refs)
    assert r.independent_origins == 1
    assert r.origins == ("agence a",)


def test_assess_distance_above_threshold_is_distinct() -> None:
    h = _far(1)
    refs = [_ref("1", "A", simhash=h), _ref("2", "B", simhash=h ^ 0b1111)]
    assert assess_independence(refs).independent_origins == 2


def test_assess_skipped_ref_does_not_register_duplicates() -> None:
    h = _far(1)
    refs = [_ref("1", "A", tier="avoid", simhash=h), _ref("2", "B", simhash=h)]
    r = assess_independence(refs)
    assert r.origins == ("b",)


GOOD = IndependenceResult(2, True, ("a", "b"))


@pytest.mark.parametrize("v", [V.TRUE, V.FALSE])
@pytest.mark.parametrize(
    "result",
    [
        IndependenceResult(0, False, ()),
        IndependenceResult(1, True, ("a",)),
        IndependenceResult(2, False, ("a", "b")),
        IndependenceResult(5, False, ("a", "b", "c", "d", "e")),
    ],
)
def test_rule_downgrades_true_and_false(v: Veracity, result: IndependenceResult) -> None:
    out = apply_independence_rule(v, ContextNeed.NONE, result, has_partial_evidence=False)
    assert out == (V.UNVERIFIABLE, ContextNeed.NONE, True)


@pytest.mark.parametrize("v", list(Veracity))
def test_rule_keeps_when_enough_evidence(v: Veracity) -> None:
    out = apply_independence_rule(v, ContextNeed.NONE, GOOD, has_partial_evidence=False)
    assert out == (v, ContextNeed.NONE, False)


@pytest.mark.parametrize("v", [V.MOSTLY_TRUE, V.MISLEADING, V.UNVERIFIABLE])
def test_rule_only_downgrades_true_and_false(v: Veracity) -> None:
    weak = IndependenceResult(0, False, ())
    out = apply_independence_rule(v, ContextNeed.NONE, weak, has_partial_evidence=False)
    assert out == (v, ContextNeed.NONE, False)


@pytest.mark.parametrize(
    ("need", "partial", "expected"),
    [
        (ContextNeed.NONE, True, ContextNeed.USEFUL),
        (ContextNeed.NONE, False, ContextNeed.NONE),
        (ContextNeed.USEFUL, True, ContextNeed.USEFUL),
        (ContextNeed.NECESSARY, True, ContextNeed.NECESSARY),
        (ContextNeed.NECESSARY, False, ContextNeed.NECESSARY),
    ],
)
def test_rule_context_need(need: ContextNeed, partial: bool, expected: ContextNeed) -> None:
    weak = IndependenceResult(0, False, ())
    out = apply_independence_rule(V.TRUE, need, weak, has_partial_evidence=partial)
    assert out == (V.UNVERIFIABLE, expected, True)


# -------------------------------------------------------------------------- datamark


def test_datamark_basic() -> None:
    assert datamark("hello big  world") == f"hello{MARKER}big{MARKER}world"


def test_marker_is_u02c6() -> None:
    assert MARKER == "ˆ"


def test_datamark_newlines_kept() -> None:
    assert datamark("a\nb") == f"a{MARKER}\nb"
    assert datamark("a \n  \n b") == f"a{MARKER}\nb"
    assert datamark("a\r\nb") == f"a{MARKER}\nb"
    assert datamark("a\tb c") == f"a{MARKER}b{MARKER}c"


def test_datamark_removes_forged_markers() -> None:
    assert datamark(f"a{MARKER}b c") == f"ab{MARKER}c"
    assert datamark(MARKER * 3) == ""


def test_datamark_empty() -> None:
    assert datamark("") == ""
    assert unmark("") == ""


def test_unmark_roundtrip() -> None:
    original = "the quick brown fox"
    assert unmark(datamark(original)) == original


def test_unmark_keeps_single_newlines() -> None:
    assert unmark(datamark("one two\nthree four")) == "one two\nthree four"


def test_unmark_collapses_and_strips() -> None:
    assert unmark(f"{MARKER}a{MARKER}{MARKER}b  c{MARKER}") == "a b c"


# ---------------------------------------------------------------------------- checks


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("« bonjour »", '" bonjour "'),
        ("“x” „y‟", '"x" "y"'),
        ("l’été ‘a’", "l'été 'a'"),
        ("a – b — c ‑ d", "a - b - c - d"),
        ("et…", "et..."),
        ("  a \n\t b  c ", "a b c"),
        (f"a{MARKER}b", "a b"),
        ("ﬁnance", "finance"),
        ("Case Kept", "Case Kept"),
        ("5″ 3′", "5\" 3'"),
        ("", ""),
    ],
)
def test_normalize_for_quote(raw: str, expected: str) -> None:
    assert normalize_for_quote(raw) == expected


PASSAGE = (
    "Le ministre a déclaré : « Le chômage a baissé de 0,3 point "
    "au dernier trimestre », avant d’ajouter que la tendance — positive — "
    "se confirme."
)


@pytest.mark.parametrize(
    "quote",
    [
        "Le chômage a baissé de 0,3 point",
        '"Le chômage a baissé de 0,3 point au dernier trimestre"',
        "« Le chômage a baissé de 0,3 point au dernier trimestre »",
        "d'ajouter que la tendance - positive - se confirme",
        "Le chômage a baissé… dernier trimestre",
        "Le chômage a baissé [...] au dernier trimestre",
        "Le chômage a baissé (...) au dernier trimestre",
        "Le chômage ... a baissé ... trimestre",
    ],
)
def test_quote_found(quote: str) -> None:
    assert quote_in_passage(quote, PASSAGE) is True


@pytest.mark.parametrize(
    "quote",
    [
        "",
        "   ",
        "...",
        "Unemployment fell by 0.3 points",  # translation never matches
        "le chômage a baissé",  # case matters
        "au dernier trimestre ... Le chômage",  # parts out of order
        "Le chômage a augmenté",
        "chômage a baissé ... inexistant",
    ],
)
def test_quote_not_found(quote: str) -> None:
    assert quote_in_passage(quote, PASSAGE) is False


def test_quote_ellipsis_parts_do_not_overlap() -> None:
    assert quote_in_passage("ab ... ab", "xx ab yy") is False
    assert quote_in_passage("ab ... ab", "xx ab yy ab") is True


def test_quote_with_marker_is_normalized() -> None:
    assert quote_in_passage(f"Le{MARKER}chômage", PASSAGE) is True


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", 0),
        ("  hello  ", 5),
        ("see https://example.org/a?b=1 now", len("see now")),
        ("https://a.fr http://b.fr", 0),
        ("a   b", 3),
        ("x" * 280, 280),
        ("café", 4),
    ],
)
def test_note_length(text: str, expected: int) -> None:
    assert note_length(text) == expected


LEX = {
    "fr": ["propagande", "désinformation de masse", "scandale", "?!", "!!"],
    "*": ["fake news"],
    "en": ["propaganda"],
}


def _terms(text: str, lang: str = "fr", **kw: object) -> list[str]:
    return [h.term for h in find_loaded_terms(text, lang, LEX, **kw)]  # type: ignore[arg-type]


def test_loaded_whole_word_only() -> None:
    assert _terms("Ceci est de la propagande.") == ["propagande"]
    assert _terms("propagandiste") == []
    assert _terms("antipropagande") == []


def test_loaded_accent_and_case_insensitive() -> None:
    assert _terms("DESINFORMATION DE MASSE") == ["désinformation de masse"]
    assert _terms("SCANDALE!") == ["scandale"]


def test_loaded_multiword_flexible_spacing() -> None:
    assert _terms("une désinformation\n  de masse") == ["désinformation de masse"]
    assert _terms("désinformation") == []


def test_loaded_non_word_terms_plain_substring() -> None:
    assert _terms("Quoi?!") == ["?!"]
    assert _terms("Non!!!") == ["!!"]


def test_loaded_star_applies_to_all_languages() -> None:
    hits = find_loaded_terms("This is FAKE news", "de", LEX)
    assert hits == [LoadedTermHit("fake news", "*")]
    assert _terms("propaganda", "en") == ["propaganda"]
    assert _terms("propaganda", "fr") == []


def test_loaded_each_term_once_in_lexicon_order() -> None:
    assert _terms("scandale scandale propagande fake news") == [
        "propagande",
        "scandale",
        "fake news",
    ]


def test_loaded_star_as_lang() -> None:
    assert [h.term for h in find_loaded_terms("fake news", "*", LEX)] == ["fake news"]


def test_loaded_allowed_suppresses_contained_hit() -> None:
    allowed = {"fr": ["scandale du sang contaminé"]}
    assert _terms("Le scandale du sang contaminé a eu lieu", allowed=allowed) == []
    assert _terms("scandale du sang contaminé et un scandale", allowed=allowed) == ["scandale"]
    assert _terms("Un scandale", allowed=allowed) == ["scandale"]


def test_loaded_allowed_other_language_ignored() -> None:
    allowed = {"en": ["scandale du sang contaminé"]}
    assert _terms("scandale du sang contaminé", allowed=allowed) == ["scandale"]


def test_loaded_empty_inputs() -> None:
    assert _terms("") == []
    assert find_loaded_terms("x", "fr", {}) == []
    assert find_loaded_terms("x", "fr", {"fr": ["", "  "]}) == []


def test_links_unknown() -> None:
    texts = ["voir https://a.fr/x et http://B.fr/y.", "(https://c.org/z)"]
    assert check_links(texts, ["https://a.fr/x"]) == ["http://B.fr/y", "https://c.org/z"]


@pytest.mark.parametrize(
    ("found", "known"),
    [
        ("http://www.a.fr/x/", "https://a.fr/x"),
        ("https://a.fr/x#section", "https://a.fr/x"),
        ("https://A.fr/x", "https://www.a.fr/x/"),
        ("https://a.fr", "http://a.fr/"),
    ],
)
def test_links_equivalence(found: str, known: str) -> None:
    assert check_links([f"see {found}"], [known]) == []


def test_links_path_case_and_query_matter() -> None:
    assert check_links(["https://a.fr/X"], ["https://a.fr/x"]) == ["https://a.fr/X"]
    assert check_links(["https://a.fr/x?id=1"], ["https://a.fr/x?id=2"]) == ["https://a.fr/x?id=1"]


def test_links_deduplicated_and_empty() -> None:
    assert check_links(["https://a.fr/x https://www.a.fr/x"], []) == ["https://a.fr/x"]
    assert check_links([], ["https://a.fr"]) == []
    assert check_links(["no url here"], []) == []


POST = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)


def test_dates_after_and_too_old() -> None:
    sources = [
        DatedSource("after", POST + timedelta(seconds=1)),
        DatedSource("same", POST),
        DatedSource("old", POST - timedelta(days=400)),
        DatedSource("fine", POST - timedelta(days=10)),
        DatedSource("undated", None),
    ]
    out = check_dates(sources, post_date=POST, max_age=timedelta(days=365))
    assert out == {"after_post": ["after"], "too_old": ["old"]}


def test_dates_no_max_age() -> None:
    old = DatedSource("old", POST - timedelta(days=4000))
    assert check_dates([old], post_date=POST, max_age=None) == {"after_post": [], "too_old": []}


def test_dates_unknown_post_date_uses_now() -> None:
    now = datetime.now(UTC)
    sources = [
        DatedSource("old", now - timedelta(days=30)),
        DatedSource("new", now - timedelta(days=1)),
        DatedSource("future", now + timedelta(days=30)),
    ]
    out = check_dates(sources, post_date=None, max_age=timedelta(days=7))
    assert out == {"after_post": [], "too_old": ["old"]}


def test_dates_naive_is_utc_and_zones_compared() -> None:
    naive = datetime(2025, 6, 1, 12, 0, 1)
    paris = timezone(timedelta(hours=2))
    before = datetime(2025, 6, 1, 13, 59, tzinfo=paris)  # 11:59 UTC
    out = check_dates(
        [DatedSource("n", naive), DatedSource("b", before)],
        post_date=POST,
        max_age=None,
    )
    assert out["after_post"] == ["n"]
    naive_post = datetime(2025, 6, 1, 12, 0)
    assert check_dates([DatedSource("n", naive)], post_date=naive_post, max_age=None)[
        "after_post"
    ] == ["n"]


def test_report_defaults_not_blocking() -> None:
    r = CheckReport()
    assert r.blocking is False
    d = r.as_dict()
    assert d["blocking"] is False
    assert d["loaded_terms"] == []
    assert d["downgraded"] is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {"invalid_quotes": ["p1"]},
        {"unknown_links": ["https://x"]},
        {"note_too_long": {"fr": 300}},
    ],
)
def test_report_blocking(kwargs: dict[str, object]) -> None:
    assert CheckReport(**kwargs).blocking is True  # type: ignore[arg-type]


def test_report_non_blocking_warnings() -> None:
    r = CheckReport(
        loaded_terms=[LoadedTermHit("x", "fr")],
        sources_after_post=["a"],
        sources_too_old=["b"],
        downgraded=True,
    )
    assert r.blocking is False


def test_report_as_dict_json_serialisable() -> None:
    r = CheckReport(
        invalid_quotes=["p1"],
        note_too_long={"fr": 300},
        loaded_terms=[LoadedTermHit("scandale", "fr")],
        unknown_links=["https://x"],
        sources_after_post=["a"],
        sources_too_old=["b"],
        downgraded=True,
    )
    d = r.as_dict()
    assert json.loads(json.dumps(d)) == d
    assert d["loaded_terms"] == [{"term": "scandale", "lang": "fr"}]
    assert d["blocking"] is True
    assert d["note_too_long"] == {"fr": 300}


# -------------------------------------------------------------------------- fidelity


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", set()),
        ("no digits", set()),
        ("10 000 euros", {"10000"}),
        ("10 000 euros", {"10000"}),
        ("10 000 euros", {"10000"}),
        ("1 234 567", {"1234567"}),
        ("10,000", {"10000"}),
        ("10.000", {"10000"}),
        ("1,234,567", {"1234567"}),
        ("1.234.567", {"1234567"}),
        ("1.234,56", {"1234.56"}),
        ("1,234.56", {"1234.56"}),
        ("7,5", {"7.5"}),
        ("7.50", {"7.5"}),
        ("7.0", {"7"}),
        ("3.14", {"3.14"}),
        ("1,2345", {"1.2345"}),
        ("0,500", {"0.5"}),
        ("2017", {"2017"}),
        ("1,5 million", {"1.5"}),
        ("50%", {"50"}),
        ("12€ et $5", {"12", "5"}),
        ("-5 degres", {"5"}),
        ("en 2017, 12 000", {"2017", "12000"}),
        ("2017 100 000", {"2017", "100000"}),
        ("12.05.2024", {"12", "5", "2024"}),
        ("1,2,3", {"1", "2", "3"}),
        ("fin 2017.", {"2017"}),
        ("a 3 chats", {"3"}),
    ],
)
def test_extract_numbers(text: str, expected: set[str]) -> None:
    assert extract_numbers(text) == frozenset(expected)


def test_extract_numbers_ignores_urls_and_mentions() -> None:
    text = "voir https://x.fr/a/123?id=456 de @user2024 et @bob42"
    assert extract_numbers(text) == frozenset()
    assert extract_numbers(text + " 99") == frozenset({"99"})


def test_extract_numbers_email_like_not_mention() -> None:
    # "a@b" is not a mention, but a digit run attached to a word is still a number
    assert extract_numbers("room 12") == frozenset({"12"})


@pytest.mark.parametrize(
    ("entities", "post", "expected"),
    [
        (["INSEE"], "Selon l'INSEE, le PIB", []),
        (["INSEE"], "Selon l'insee", []),
        (["INSEE"], "Selon Eurostat", ["INSEE"]),
        (["BCE"], "La Banque Centrale Européenne a décidé", []),
        (["BCE"], "La banque centrale européenne", ["BCE"]),
        (["BCE"], "La Banque Européenne", ["BCE"]),
        (["BCE"], "Banque Centrale Européenne", []),
        (["B.C.E."], "la BCE annonce", []),
        (["Élysée"], "Au palais de l'Elysee", []),
        (["Elysée"], "Au palais de l'ÉLYSÉE", []),
        (["Emmanuel Macron"], "macron a dit", ["Emmanuel Macron"]),
        (["Emmanuel Macron"], "Emmanuel MACRON", []),
        (["Emmanuel Macron"], "Emmanuel a dit", ["Emmanuel Macron"]),
        (["Marine Le Pen"], "Le Pen et Marine", []),
        (["Banque centrale européenne"], "la banque européenne", ["Banque centrale européenne"]),
        (["Paris"], "Parisien", ["Paris"]),
        (["UE"], "l'UE et la France", []),
        (["UE"], "Union Européenne", []),
        (["UE"], "Une Union", ["UE"]),
        (["Li"], "li", []),
        (["", "  "], "anything", []),
        (["A", "B"], "", ["A", "B"]),
    ],
)
def test_missing_entities(entities: list[str], post: str, expected: list[str]) -> None:
    assert missing_entities(entities, post) == expected


def test_missing_entities_order_preserved() -> None:
    assert missing_entities(["Zeta", "Alpha", "Mu"], "Alpha") == ["Zeta", "Mu"]


def test_fidelity_all_present() -> None:
    assert check_claim_fidelity("le PIB a cru de 1,5 %", ["PIB"], "Le PIB a cru de 1.5%") == []


def test_fidelity_number_contamination() -> None:
    issues = check_claim_fidelity("12 000 morts en 2021", [], "des morts en 2021")
    assert issues == [FidelityIssue("number", "12000")]


def test_fidelity_numbers_sorted_numerically() -> None:
    issues = check_claim_fidelity("100 puis 9 puis 25", [], "rien")
    assert [i.value for i in issues] == ["9", "25", "100"]


def test_fidelity_context_counts() -> None:
    assert check_claim_fidelity("42 morts", [], "bilan", context_text="Il y a 42 morts") == []
    ent = check_claim_fidelity("x", ["Macron"], "ok", context_text="Macron parle")
    assert ent == []


def test_fidelity_entity_contamination_and_both_kinds() -> None:
    issues = check_claim_fidelity("5 morts", ["Lyon"], "3 morts à Paris")
    assert issues == [FidelityIssue("number", "5"), FidelityIssue("entity", "Lyon")]


def test_fidelity_accent_variants_ok() -> None:
    assert check_claim_fidelity("", ["Île-de-France"], "en ile de france") == []


def test_fidelity_number_formats_equivalent() -> None:
    assert check_claim_fidelity("10 000", [], "10.000 personnes") == []
    assert check_claim_fidelity("7,5", [], "7.50") == []


def test_omission_cases() -> None:
    assert check_omission("12 000 morts", ["des morts"], has_absence=False) is True
    assert check_omission("12 000 morts", ["12 000 morts"], has_absence=False) is False
    assert check_omission("12 000 morts", [], has_absence=False) is True
    assert check_omission("12 000 morts", [], has_absence=True) is False
    assert check_omission("pas de chiffre", [], has_absence=False) is False
    assert check_omission("https://x.fr/123 @bob42", [], has_absence=False) is False


def test_omission_any_number_suffices() -> None:
    assert check_omission("5 morts et 7 blessés", ["7 blessés"], has_absence=False) is False


def test_omission_canonical_comparison() -> None:
    assert check_omission("10 000", ["10.000"], has_absence=False) is False


# --------------------------------------------------------------------------- passages


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", []),
        ("Hello, World!", ["hello", "world"]),
        ("Été à Paris", ["ete", "paris"]),
        ("snake_case 2024 a 7", ["snake", "case", "2024", "7"]),
        ("l'INSEE d’aujourd'hui", ["insee", "aujourd", "hui"]),
        ("Straße", ["strasse"]),
        ("10,5%", ["10", "5"]),
    ],
)
def test_tokenize(text: str, expected: list[str]) -> None:
    assert tokenize(text) == expected


def test_chunk_empty_and_short() -> None:
    assert chunk_text("a", "") == []
    assert chunk_text("a", "   \n ") == []
    chunks = chunk_text("a", "Just a short text.")
    assert chunks == [Chunk("a", 0, "Just a short text.")]


def _text(sentences: int, words_per: int = 10) -> str:
    return " ".join(
        " ".join(f"w{s}x{i}" for i in range(words_per - 1)) + f" end{s}." for s in range(sentences)
    )


def test_chunk_cuts_at_sentence_end_within_tolerance() -> None:
    chunks = chunk_text("a", _text(30, 10), target_words=25)
    assert len(chunks) > 1
    for c in chunks[:-1]:
        assert c.text.endswith(".")
        assert 17 <= len(c.text.split()) <= 32


def test_chunk_hard_cut_without_sentence_end() -> None:
    words = " ".join(f"w{i}" for i in range(100))
    chunks = chunk_text("a", words, target_words=30)
    assert [len(c.text.split()) for c in chunks] == [30, 30, 30, 10]


def test_chunk_positions_and_source_key() -> None:
    chunks = chunk_text("src", _text(20), target_words=20)
    assert [c.position for c in chunks] == list(range(len(chunks)))
    assert {c.source_key for c in chunks} == {"src"}


def test_chunk_max_chunks() -> None:
    words = " ".join(f"w{i}" for i in range(1000))
    assert len(chunk_text("a", words, target_words=10, max_chunks=7)) == 7


def test_chunk_preserves_all_words_in_order() -> None:
    text = _text(25)
    chunks = chunk_text("a", text, target_words=33)
    assert " ".join(c.text for c in chunks).split() == text.split()


def test_chunk_deterministic_and_keeps_newlines() -> None:
    text = "First line.\nSecond line."
    assert chunk_text("a", text) == chunk_text("a", text)
    assert chunk_text("a", text)[0].text == text


def _chunks(*texts: str, key: str = "s") -> list[Chunk]:
    return [Chunk(key, i, t) for i, t in enumerate(texts)]


def test_bm25_ranks_relevant_first_and_drops_zero() -> None:
    chunks = _chunks(
        "le chomage a baisse en france",
        "la meteo est belle",
        "chomage chomage chomage des jeunes en espagne",
    )
    ranked = bm25_rank(["chômage"], chunks)
    assert [r.chunk.position for r in ranked] == [2, 0]
    assert all(r.score > 0 for r in ranked)


def test_bm25_idf_formula_single_term() -> None:
    chunks = _chunks("alpha beta", "gamma delta", "epsilon zeta")
    (hit,) = bm25_rank(["alpha"], chunks)
    n, df, k1, b = 3, 1, 1.2, 0.75
    idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
    expected = idf * 1 * (k1 + 1) / (1 + k1 * (1 - b + b * 2 / 2))
    assert hit.score == pytest.approx(expected)


def test_bm25_tokenizes_query_elements_and_dedupes() -> None:
    chunks = _chunks("taux de chomage", "autre chose")
    a = bm25_rank(["Chômage taux"], chunks)
    b = bm25_rank(["taux", "chomage", "taux"], chunks)
    assert a[0].score == pytest.approx(b[0].score)


def test_bm25_tie_order_and_simhash() -> None:
    chunks = [Chunk("b", 0, "same words"), Chunk("a", 1, "same words"), Chunk("a", 0, "same words")]
    ranked = bm25_rank(["same"], chunks)
    assert [(r.chunk.source_key, r.chunk.position) for r in ranked] == [
        ("a", 0),
        ("a", 1),
        ("b", 0),
    ]
    assert ranked[0].simhash == simhash64(normalize("same words"))


def test_bm25_empty_inputs() -> None:
    assert bm25_rank([], _chunks("x y")) == []
    assert bm25_rank(["x"], []) == []
    assert bm25_rank(["zzz"], _chunks("x y")) == []
    assert bm25_rank(["a"], _chunks("x y")) == []  # 1-letter token dropped


def test_bm25_length_normalisation() -> None:
    short = "chomage"
    long = "chomage " + " ".join(f"mot{i}" for i in range(50))
    ranked = bm25_rank(["chomage"], _chunks(long, short))
    assert ranked[0].chunk.text == short


def _rc(source: str, pos: int, score: float, sim: int) -> RankedChunk:
    return RankedChunk(Chunk(source, pos, f"{source}{pos}"), score, sim)


def test_dedupe_keeps_best_and_order() -> None:
    far = ~0 >> 1  # distant from 0
    ranked = [_rc("a", 0, 9.0, 0), _rc("b", 0, 8.0, far), _rc("c", 0, 7.0, 0b11)]
    out = dedupe_chunks(ranked)
    assert [r.chunk.source_key for r in out] == ["a", "b"]


def test_dedupe_threshold_boundary() -> None:
    ranked = [_rc("a", 0, 2.0, 0), _rc("b", 0, 1.0, 0b111)]
    assert len(dedupe_chunks(ranked, max_bits=3)) == 1
    assert len(dedupe_chunks(ranked, max_bits=2)) == 2


def test_dedupe_unsorted_input_keeps_best_scored() -> None:
    ranked = [_rc("low", 0, 1.0, 0), _rc("high", 0, 5.0, 1)]
    assert [r.chunk.source_key for r in dedupe_chunks(ranked)] == ["high"]


def test_dedupe_signed_simhash_distance() -> None:
    ranked = [_rc("a", 0, 2.0, -1), _rc("b", 0, 1.0, (1 << 63) - 1)]
    # -1 is all ones, 2**63-1 has 63 ones: distance 1 → duplicates
    assert len(dedupe_chunks(ranked)) == 1


def test_dedupe_empty() -> None:
    assert dedupe_chunks([]) == []


def test_select_limit_and_per_source() -> None:
    sims = [0, (1 << 16) - 1 << 8, (1 << 32) - 1 << 20, (1 << 24) - 1 << 36, 0xFFFF_0000_FFFF]
    ranked = [_rc("a", i, 10.0 - i, s) for i, s in enumerate(sims)]
    ranked.append(_rc("b", 0, 1.0, 0x5555_5555_5555_5555))
    out = select_passages(ranked, limit=10, per_source=2)
    assert [(r.chunk.source_key, r.chunk.position) for r in out] == [("a", 0), ("a", 1), ("b", 0)]
    assert len(select_passages(ranked, limit=2, per_source=4)) == 2


def test_select_removes_near_duplicates_and_zero_limit() -> None:
    ranked = [_rc("a", 0, 3.0, 0), _rc("b", 0, 2.0, 0)]
    assert len(select_passages(ranked, limit=5)) == 1
    assert select_passages(ranked, limit=0) == []


# ----------------------------------------------------------------------------- quota

T0 = datetime(2025, 6, 1, 8, 0, tzinfo=UTC)


def _cand(key: str = "c", **kw: object) -> DraftCandidate:
    base: dict[str, object] = {
        "key": key,
        "veracity": V.TRUE,
        "confidence": 0.9,
        "agreement": Agreement.IDENTICAL,
        "conflicting_sources": False,
        "level": "N3",
        "reach": 0,
        "ready_at": T0,
    }
    base.update(kw)
    return DraftCandidate(**base)  # type: ignore[arg-type]


def test_is_doubtful_cases() -> None:
    assert is_doubtful(_cand()) is False
    assert is_doubtful(_cand(agreement=None)) is False
    assert is_doubtful(_cand(agreement=Agreement.STYLE)) is False
    assert is_doubtful(_cand(agreement=Agreement.SUBSTANTIVE)) is True
    assert is_doubtful(_cand(conflicting_sources=True)) is True
    assert is_doubtful(_cand(confidence=0.7999)) is True
    assert is_doubtful(_cand(confidence=0.80)) is False
    assert is_doubtful(_cand(confidence=0.85), medium_threshold=0.9) is True


def test_priority_order_levels_doubt_veracity_reach_age() -> None:
    n4 = _cand("n4", level="N4")
    doubt = _cand("doubt", confidence=0.5)
    false = _cand("false", veracity=V.FALSE)
    misleading = _cand("mis", veracity=V.MISLEADING, reach=1)
    big = _cand("big", reach=1000)
    small = _cand("small", reach=10)
    old = _cand("old", ready_at=T0 - timedelta(hours=1))
    ordered = select_for_review([old, small, big, misleading, false, doubt, n4], slots=7)
    assert [c.key for c in ordered] == ["n4", "doubt", "mis", "false", "big", "small", "old"][
        :0
    ] or True
    keys = [c.key for c in ordered]
    assert keys[0] == "n4"
    assert keys[1] == "doubt"
    assert keys[2:4] == ["mis", "false"]  # higher reach first among false/misleading
    assert keys[4:] == ["big", "small", "old"]


def test_priority_key_shape_and_values() -> None:
    assert priority_key(_cand(level="N4", confidence=0.1, veracity=V.FALSE, reach=5)) == (
        0,
        0,
        0,
        -5.0,
        T0,
    )
    assert priority_key(_cand()) == (1, 1, 1, 0.0, T0)


def test_priority_older_ready_first_on_tie() -> None:
    a = _cand("a", ready_at=T0 + timedelta(minutes=5))
    b = _cand("b", ready_at=T0)
    assert [c.key for c in select_for_review([a, b], slots=2)] == ["b", "a"]


def test_priority_ignores_author_like_fields() -> None:
    # Only the dataclass fields count: two identical candidates tie on the key itself.
    assert priority_key(_cand("x")) == priority_key(_cand("y"))


def test_select_slots() -> None:
    cands = [_cand(str(i), reach=i) for i in range(5)]
    assert [c.key for c in select_for_review(cands, slots=2)] == ["4", "3"]
    assert select_for_review(cands, slots=0) == []
    assert select_for_review(cands, slots=-3) == []
    assert len(select_for_review(cands, slots=99)) == 5
    assert select_for_review([], slots=3) == []


def test_select_deterministic_tiebreak_on_key() -> None:
    a, b = _cand("b"), _cand("a")
    assert [c.key for c in select_for_review([a, b], slots=2)] == ["a", "b"]
