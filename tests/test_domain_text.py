from __future__ import annotations

from tower.core.domain.text import (
    content_hash,
    hamming,
    normalize,
    simhash64,
    simhash_bands,
    useful_word_count,
)

LONG_TEXT = (
    "the government announced yesterday that the pension reform will be presented to "
    "parliament before the end of the year despite sharp criticism from the opposition "
    "and from several unions who denounce an unfair text for workers across the whole country"
)


def test_normalize_nfkc_and_lowercase() -> None:
    assert normalize("ＨＥＬＬＯ Ｗorld ﬁne") == "hello world fine"


def test_normalize_removes_tracking_params_keeps_others() -> None:
    url = "https://Example.com/a?id=3&utm_source=x&fbclid=abc&utm_medium=y&q=z"
    assert normalize(f"see {url}") == "see https://example.com/a?id=3&q=z"


def test_normalize_drops_trailing_question_mark_and_fragment() -> None:
    assert normalize("https://a.com/p?utm_campaign=1&gclid=2") == "https://a.com/p"
    assert normalize("https://a.com/p#section-2") == "https://a.com/p"
    assert normalize("https://a.com/p?x=1#top") == "https://a.com/p?x=1"
    assert normalize("https://a.com/p?") == "https://a.com/p"


def test_normalize_mentions() -> None:
    assert normalize("Hi @SomeOne and @other_1!") == "hi @user and @user!"
    assert normalize("mail me at a@b.com") == "mail me at a@b.com"
    assert normalize("https://medium.com/@Foo/post") == "https://medium.com/@foo/post"


def test_normalize_emoji_removed_but_euro_and_punctuation_kept() -> None:
    assert normalize("Great 😀🔥 deal: 20€ only!") == "great deal: 20€ only!"
    assert normalize("family 👨‍👩‍👧 ❤️ ok") == "family ok"
    assert normalize("100$ (£5) — « oui »") == "100$ (£5) — « oui »"


def test_normalize_whitespace() -> None:
    assert normalize("  a \t b\n\n c  ") == "a b c"
    assert normalize("") == ""


def test_useful_word_count_basic() -> None:
    assert useful_word_count("Hello brave new world") == 4


def test_useful_word_count_ignores_urls_mentions_short_tokens_numbers() -> None:
    text = "@bob look https://example.com/a-long-path a b I 2024 x1 ok"
    assert useful_word_count(text) == 2  # "look", "ok"


def test_useful_word_count_hashtags() -> None:
    assert useful_word_count("#retraites #a") == 1


def test_useful_word_count_cjk() -> None:
    assert useful_word_count("今天天气很好") == 3
    assert useful_word_count("今天天气很好 and more") == 5
    assert useful_word_count("ありがとう") == 2


def test_content_hash_length_and_determinism() -> None:
    h = content_hash("hello")
    assert isinstance(h, bytes)
    assert len(h) == 32
    assert h == content_hash("hello")
    assert h != content_hash("hello!")


def test_simhash_signed_range_and_determinism() -> None:
    values = [simhash64(normalize(t)) for t in (LONG_TEXT, "short text here", "autre texte")]
    for v in values:
        assert -(2**63) <= v < 2**63
    assert values == [
        simhash64(normalize(t)) for t in (LONG_TEXT, "short text here", "autre texte")
    ]
    assert any(v < 0 for v in [simhash64(str(i)) for i in range(40)])


def test_simhash_empty_is_zero() -> None:
    assert simhash64("") == 0
    assert simhash64("   !!! ") == 0


def test_simhash_invariant_to_cosmetic_differences() -> None:
    base = "Breaking news about the reform https://example.com/a?id=1 @alice thanks"
    variants = [
        "Breaking news about the reform https://example.com/a?id=1&utm_source=tw&fbclid=zz "
        "@alice thanks",
        "Breaking news about the reform https://example.com/a?id=1 @someone_else thanks",
        "Breaking news about the reform 🔥 https://example.com/a?id=1 @alice thanks 😀",
        "BREAKING NEWS ABOUT THE REFORM https://example.com/a?id=1 @ALICE THANKS",
        "  Breaking   news\n about the  reform https://example.com/a?id=1#frag @alice\tthanks ",
    ]
    reference = simhash64(normalize(base))
    for variant in variants:
        assert hamming(reference, simhash64(normalize(variant))) == 0


def _distance(a: str, b: str) -> int:
    return hamming(simhash64(normalize(a)), simhash64(normalize(b)))


def test_simhash_one_word_appended_is_close() -> None:
    assert len(LONG_TEXT.split()) >= 40
    distances = [
        _distance(LONG_TEXT, f"{LONG_TEXT} {w}") for w in ("today", "really", "apparently")
    ]
    assert max(distances) <= 6


def test_simhash_one_word_replaced_is_close() -> None:
    words = LONG_TEXT.split()
    distances = []
    for index, replacement in ((10, "tomorrow"), (25, "weak"), (35, "entire")):
        edited = [*words[:index], replacement, *words[index + 1 :]]
        distances.append(_distance(LONG_TEXT, " ".join(edited)))
    # A replacement changes 3 features (word + 2 bigrams) instead of 2 for an append,
    # so the bound is slightly looser (measured: 5, 7, 4).
    assert max(distances) <= 8


def test_simhash_unrelated_texts_are_far() -> None:
    other = (
        "le chat dort paisiblement sur le canapé du salon pendant que la pluie tombe "
        "doucement dehors et que les enfants jouent aux cartes dans la cuisine voisine"
    )
    assert _distance(LONG_TEXT, other) > 10


def test_simhash_bands_matches_manual_extraction() -> None:
    for value in (simhash64(LONG_TEXT), -1, -(2**63), 0, 2**63 - 1, 0x1234_5678_9ABC_DEF0):
        bands = simhash_bands(value)
        unsigned = value & (2**64 - 1)
        assert bands == tuple((unsigned >> (16 * i)) & 0xFFFF for i in range(4))
        assert sum(b << (16 * i) for i, b in enumerate(bands)) == unsigned


def test_simhash_bands_known_values() -> None:
    assert simhash_bands(-1) == (0xFFFF,) * 4
    assert simhash_bands(0x1234_5678_9ABC_DEF0) == (0xDEF0, 0x9ABC, 0x5678, 0x1234)


def test_hamming() -> None:
    assert hamming(5, 5) == 0
    assert hamming(0b1010, 0b0110) == 2
    assert hamming(0, -1) == 64
    assert hamming(0, 2**64 - 1) == 64
    assert hamming(-1, 2**64 - 1) == 0
    assert hamming(12345, -987654321) == hamming(-987654321, 12345)


def test_simhash_ignores_added_links_and_mentions() -> None:
    base = (
        "Le gouvernement annonce que le taux de chomage a baisse de deux points cette "
        "annee selon les derniers chiffres officiels publies ce matin par l institut"
    )
    copy = f"{base} https://example.org/?utm_source=x @someone"
    a, b = normalize(base), normalize(copy)
    assert content_hash(a) != content_hash(b)
    assert hamming(simhash64(a), simhash64(b)) == 0
