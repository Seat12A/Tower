"""Pure rules of narratives and waves: tiers, state machine, scores, medoids, attachment,
fingerprints, sampling (no database)."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from tower.core.domain.factcheck.waves import (
    AttachAnchors,
    AttachSignals,
    WaveThresholds,
    WindowStats,
    cooccurrence_met,
    elect_medoids,
    engagement_of,
    entity_key,
    evaluate_attachment,
    expected_count,
    extract_links,
    find_bridge,
    growth,
    next_state,
    novelty_fingerprints,
    novelty_takes,
    parse_thresholds,
    percentile,
    poisson_score,
    sampling_rate,
    stratified_sample,
    tier_of,
)

T = WaveThresholds()
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


def rng(seed: int) -> random.Random:
    return random.Random(seed)  # noqa: S311 (tests: reproducible sampling)


def stats(posts: int, flow: int = 1000, accounts: int = 50, **kw: float) -> WindowStats:
    return WindowStats(posts=posts, flow=flow, accounts=accounts, **kw)  # type: ignore[arg-type]


# -------------------------------------------------------------------------- tiers


def test_below_the_watch_floors_nothing_happens() -> None:
    assert tier_of(stats(5), T) == "active"  # 0.5 % of the flow
    assert tier_of(stats(20, accounts=9), T) == "active"  # not enough distinct accounts


def test_watch_needs_share_and_accounts() -> None:
    assert tier_of(stats(10, accounts=10), T) == "watch"  # exactly 1 % and 10 accounts


def test_surge_needs_share_accounts_and_growth() -> None:
    assert tier_of(stats(30, accounts=30, growth=2.0), T) == "surge"
    assert tier_of(stats(30, accounts=30, growth=1.9), T) == "watch"
    assert tier_of(stats(30, accounts=29, growth=5.0), T) == "watch"
    assert tier_of(stats(29, accounts=40, growth=5.0), T) == "watch"


def test_growth_cannot_trigger_a_surge_while_the_bases_are_frozen() -> None:
    wave = stats(60, accounts=40, growth=10.0)
    assert tier_of(wave, T) == "surge"
    assert tier_of(wave, T, growth_frozen=True) == "watch"


def test_watch_on_two_platforms_is_a_surge_without_the_share_floor() -> None:
    assert tier_of(stats(10, accounts=10, platforms_at_watch=2), T) == "surge"
    assert tier_of(stats(10, accounts=10, platforms_at_watch=1), T) == "watch"
    # a lone post on two platforms is not a wave: the watch floors still apply
    assert tier_of(stats(2, accounts=2, platforms_at_watch=2), T) == "active"


def test_a_group_at_n3_makes_a_watched_narrative_a_surge() -> None:
    assert tier_of(stats(10, accounts=10), T, has_n3_group=True) == "surge"
    assert tier_of(stats(2, accounts=2), T, has_n3_group=True) == "active"


def test_alert_by_share_or_by_watched_theme() -> None:
    assert tier_of(stats(80, accounts=80), T) == "alert"
    assert tier_of(stats(80, accounts=79, growth=9.0), T) == "surge"
    assert tier_of(stats(10, accounts=10), T, watched_theme=True) == "alert"
    assert tier_of(stats(2, accounts=2), T, watched_theme=True) == "active"


def test_custom_thresholds_come_from_the_json_setting() -> None:
    parsed = parse_thresholds('{"watch": {"share": 0.5}, "alert": {"accounts": 5}}')
    assert parsed.watch_share == 0.5 and parsed.watch_accounts == 10
    assert parsed.alert_accounts == 5 and parsed.surge_growth == 2.0
    assert parse_thresholds("{}") == T


@pytest.mark.parametrize("raw", ["nope", "[]", '{"watch": 3}', '{"watch": {"share": "x"}}'])
def test_invalid_threshold_json_is_rejected(raw: str) -> None:
    with pytest.raises(ValueError, match="wave thresholds"):
        parse_thresholds(raw)


# ------------------------------------------------------------------ state machine


def test_a_narrative_rises_immediately() -> None:
    assert next_state("active", "surge", None, NOW, end_hours=6) == ("surge", None)
    assert next_state("surge", "alert", NOW, NOW, end_hours=6) == ("alert", None)


def test_watch_falls_back_at_once() -> None:
    assert next_state("watch", "active", None, NOW, end_hours=6) == ("active", None)


def test_a_surge_ends_only_after_the_hold_time_below_watch() -> None:
    assert next_state("surge", "active", None, NOW, end_hours=6) == ("surge", NOW)
    since = NOW - timedelta(hours=5, minutes=59)
    assert next_state("surge", "active", since, NOW, end_hours=6) == ("surge", since)
    assert next_state("surge", "active", NOW - timedelta(hours=6), NOW, end_hours=6) == (
        "active",
        None,
    )
    # back above watch: the clock restarts
    assert next_state("alert", "watch", since, NOW, end_hours=6) == ("alert", None)


# ------------------------------------------------------------------------- scores


def test_poisson_score_is_defined_on_sparse_counts() -> None:
    assert poisson_score(0, 0.0) == 0.0
    assert poisson_score(10, 0.0) == pytest.approx(10 / 5**0.5)
    assert poisson_score(3, 3.0) == 0.0
    assert poisson_score(0, 4.0) < 0  # fewer than expected


def test_expected_count_scales_the_previous_window_by_the_flow() -> None:
    assert expected_count(10, 200, 100) == 20.0
    assert expected_count(10, 100, 0) == 10.0  # no flow in the previous window


def test_growth_floors_the_previous_window_at_one() -> None:
    assert growth(60, 30) == 2.0
    assert growth(5, 0) == 5.0


# ------------------------------------------------------------------------ medoids


def test_medoids_are_the_founder_then_the_two_highest_reach() -> None:
    assert elect_medoids(1, [(1, 0), (2, 50), (3, 90), (4, 70)]) == [1, 3, 4]


def test_the_founder_stays_even_with_the_lowest_reach_and_ties_go_to_the_lowest_id() -> None:
    assert elect_medoids(9, [(9, 0), (5, 10), (4, 10), (3, 10)]) == [9, 3, 4]
    assert elect_medoids(1, [(1, 5)]) == [1]


# --------------------------------------------------------------------- attachment

SEM = AttachSignals(semantic=True)
ENT = AttachSignals(entity=True)
MEDIA = AttachSignals(media=True)
RARE = AttachAnchors(rare_entity=True)


def rule(signals: AttachSignals, anchors: AttachAnchors, sim: float = 0.9) -> bool:
    return evaluate_attachment(signals, anchors, medoid_similarity=sim, medoid_floor=0.6)


def test_two_signals_one_non_semantic_one_anchor_and_the_floor_attach() -> None:
    assert rule(AttachSignals(semantic=True, entity=True), RARE)
    assert rule(AttachSignals(semantic=True, media=True), AttachAnchors(specific_media=True))
    assert rule(AttachSignals(cooccurrence=True, entity=True), AttachAnchors(shared_link=True))


@pytest.mark.parametrize(
    "signals",
    [SEM, ENT, MEDIA, AttachSignals(), AttachSignals(cooccurrence=True)],
    ids=["semantic only", "entity only", "media only", "none", "co-occurrence only"],
)
def test_a_single_signal_never_attaches(signals: AttachSignals) -> None:
    assert not rule(signals, RARE)


def test_two_signals_need_a_non_semantic_one() -> None:
    # semantic is one signal: two semantic-only signals cannot exist, so the rule is about
    # the non-semantic requirement being checked independently of the count
    assert not evaluate_attachment(
        AttachSignals(semantic=True), RARE, medoid_similarity=0.9, medoid_floor=0.6
    )


def test_without_an_anchor_the_group_does_not_attach() -> None:
    assert not rule(AttachSignals(semantic=True, entity=True), AttachAnchors())


def test_below_the_medoid_floor_the_group_does_not_attach() -> None:
    both = AttachSignals(semantic=True, entity=True)
    assert not rule(both, RARE, sim=0.59)
    assert rule(both, RARE, sim=0.60)


def test_each_anchor_alone_is_enough() -> None:
    both = AttachSignals(semantic=True, entity=True)
    for anchors in (
        AttachAnchors(rare_entity=True),
        AttachAnchors(specific_media=True),
        AttachAnchors(shared_link=True),
    ):
        assert rule(both, anchors)


def test_cooccurrence_needs_three_posts_from_more_than_three_accounts() -> None:
    assert cooccurrence_met(3, 4)
    assert not cooccurrence_met(2, 9)
    assert not cooccurrence_met(9, 3)


def test_entity_key_prefers_the_qid_else_a_folded_name() -> None:
    assert entity_key("Anything", "q42") == "Q42"
    assert entity_key("  Émmanuel  MACRON ", None) == "emmanuel macron"
    assert entity_key("L'Élysée", None) == entity_key("l elysee", None)


def test_links_are_normalized_without_scheme_query_or_trailing_punctuation() -> None:
    text = "See https://www.Example.org/a/b/?utm=1#x, and http://example.org/a/b. no.link"
    assert extract_links(text) == {"example.org/a/b"}
    assert extract_links("go to https://news.fr/") == {"news.fr"}
    assert extract_links("nothing here") == set()


def test_a_bridge_group_between_two_subsets_is_detected() -> None:
    nodes = [1, 2, 3, 4, 5]
    assert find_bridge(nodes, [(1, 2), (2, 3), (3, 4), (4, 5)])  # 3 splits {1,2} and {4,5}
    assert not find_bridge(nodes, [(1, 2), (2, 3), (3, 4), (4, 5), (5, 1)])  # a cycle
    assert not find_bridge([1, 2, 3], [(1, 2), (2, 3)])  # too small to be two subsets
    assert not find_bridge(nodes, [(1, 2), (1, 3), (1, 4), (1, 5)])  # star: singletons only


# ------------------------------------------------------------- fingerprints, sampling


def test_fingerprints_cover_numbers_links_mentions_media_and_entities() -> None:
    fps = dict.fromkeys(
        novelty_fingerprints(
            "Selon @Alice_B, le maire de Lyon, Jean Dupont a dit 1 250 000 euros "
            "via https://www.site.org/x?a=1 !",
            media_hashes=["ABCD"],
        )
    )
    assert ("number", "number:1250000") in fps
    assert ("link", "link:site.org/x") in fps
    assert ("mention", "mention:alice_b") in fps
    assert ("media", "media:abcd") in fps
    assert ("entity", "entity:jean dupont") in fps
    assert ("entity", "entity:lyon") in fps
    # the first word of a sentence is not an entity, lone digits are noise
    assert ("entity", "entity:selon") not in fps
    assert not any(fp.endswith(":1") for _, fp in fps)


def test_fingerprints_are_deterministic_and_deduplicated() -> None:
    text = "Total 40,5 puis 40,5 et 40,5 encore."
    first = novelty_fingerprints(text)
    assert first == novelty_fingerprints(text)
    assert first.count(("number", "number:40.5")) == 1


def test_a_fingerprint_takes_with_enough_posts_and_accounts() -> None:
    assert novelty_takes(3, 3, min_posts=3, min_accounts=3)
    assert not novelty_takes(3, 2, min_posts=3, min_accounts=3)
    assert not novelty_takes(2, 9, min_posts=3, min_accounts=3)


def test_sampling_rate_is_one_until_the_limit_then_limit_over_rate() -> None:
    assert sampling_rate(150, 200) == 1.0
    assert sampling_rate(200, 200) == 1.0
    assert sampling_rate(800, 200) == 0.25
    assert sampling_rate(0, 200) == 1.0


def test_stratified_sample_keeps_every_stratum_and_the_rate() -> None:
    items = [(i, ("x", "h1")) for i in range(40)] + [(100 + i, ("bluesky", "h1")) for i in range(2)]
    kept, dropped = stratified_sample(items, 0.25, rng(7))
    assert sorted(kept + dropped) == sorted(i for i, _ in items)
    assert len([i for i in kept if i < 100]) == 10  # round(40 * 0.25)
    assert len([i for i in kept if i >= 100]) == 1  # a small stratum keeps at least one
    again = stratified_sample(items, 0.25, rng(7))
    assert again == (kept, dropped)  # seeded: reproducible
    assert stratified_sample(items, 1.0, rng(1)) == ([i for i, _ in items], [])


def test_percentile_and_engagement() -> None:
    assert percentile([], 0.9) == 0.0
    assert percentile([10], 0.9) == 10
    assert percentile(list(range(11)), 0.9) == pytest.approx(9.0)
    assert engagement_of({"impressions": 500, "likes": 9}) == 500
    assert engagement_of({"likes": 4, "reposts": 6}) == 10
    assert engagement_of(None) == 0
    assert engagement_of({"impressions": "many"}) == 0
