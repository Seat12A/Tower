"""Text normalisation and fingerprints (E0). Standard library only.

The original text is always stored unchanged; these functions only feed the
fingerprints, the language detection and the useful word count.
"""

from __future__ import annotations

import hashlib
import itertools
import re
import unicodedata

TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "igshid",
        "ref_src",
        "ref_url",
        "s",
        "t",
        "si",
        "_ga",
        "yclid",
        "twclid",
    }
)
"""Query parameters removed from URLs, in addition to every ``utm_*`` parameter."""

_URL_OR_MENTION = re.compile(r"(https?://[^\s]+)|(?<![\w@])@\w+")
_MENTION_PLACEHOLDER = "@user"
_WS = re.compile(r"\s+")
_EDGE_PUNCT = re.compile(r"^\W+|\W+$")
_LETTER_RUN = re.compile(r"[^\W\d_]+")
_REMOVED_CHARS = frozenset("\ufe0e\ufe0f\u200d\u20e3")
_REMOVED_CATEGORIES = frozenset({"So", "Sk"})
_SPACELESS_RANGES = (
    (0x0E00, 0x0EFF),  # Thai, Lao
    (0x1000, 0x109F),  # Myanmar
    (0x1780, 0x17FF),  # Khmer
    (0x3040, 0x30FF),  # Hiragana, Katakana
    (0x31F0, 0x31FF),
    (0x3400, 0x4DBF),  # Han
    (0x4E00, 0x9FFF),
    (0x20000, 0x2FFFF),
)

SIMHASH_BANDS = 4
SIMHASH_BAND_BITS = 16


def _is_spaceless(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _SPACELESS_RANGES)


def _clean_url(url: str) -> str:
    url = url.split("#", 1)[0]
    base, sep, query = url.partition("?")
    if not sep:
        return base
    kept = []
    for param in query.split("&"):
        key = param.split("=", 1)[0]
        if not param or key.startswith("utm_") or key in TRACKING_PARAMS:
            continue
        kept.append(param)
    return f"{base}?{'&'.join(kept)}" if kept else base


def _replace_match(m: re.Match[str]) -> str:
    return _clean_url(m.group(1)) if m.group(1) else _MENTION_PLACEHOLDER


def normalize(text: str) -> str:
    """Canonical form used for fingerprints.

    In order: Unicode NFKC; lowercase (``str.casefold`` is NOT used: keep ``lower``);
    URLs (``http://`` / ``https://``) kept but with tracking parameters removed
    (``utm_*`` and ``TRACKING_PARAMS``), fragment removed, trailing ``?`` dropped;
    mentions ``@handle`` replaced by ``@user``; decorative emoji and pictographs removed
    (Unicode categories ``So``/``Sk`` and variation selectors / ZWJ, but keep letters,
    digits, punctuation and currency symbols); whitespace runs compacted to one space;
    result stripped.
    """
    text = unicodedata.normalize("NFKC", text).lower()
    text = _URL_OR_MENTION.sub(_replace_match, text)
    if not text.isascii():
        text = "".join(
            ch
            for ch in text
            if ch not in _REMOVED_CHARS and unicodedata.category(ch) not in _REMOVED_CATEGORIES
        )
    return _WS.sub(" ", text).strip()


def useful_word_count(text: str) -> int:
    """Number of useful words in ``normalize(text)``: tokens with at least two letters
    (``str.isalpha`` on at least two characters), excluding URLs and ``@user``.
    Hashtags count by their word (``#retraites`` → one word). For scripts without
    spaces (CJK, Thai), each run of letters counts one word per 2 characters."""
    count = 0
    for token in normalize(text).split(" "):
        if not token or token.startswith(("http://", "https://")) or token == _MENTION_PLACEHOLDER:
            continue
        token = token.lstrip("#")
        if sum(1 for ch in token if ch.isalpha()) < 2:
            continue
        if token.isascii() or not any(_is_spaceless(ch) for ch in token):
            count += 1
            continue
        for run in _LETTER_RUN.findall(token):
            if _is_spaceless(run[0]):
                count += len(run) // 2
            elif len(run) >= 2:
                count += 1
    return count


def content_hash(normalized: str) -> bytes:
    """SHA-256 of the UTF-8 normalised text (32 bytes)."""
    return hashlib.sha256(normalized.encode("utf-8")).digest()


def _is_noise(token: str) -> bool:
    return token.startswith(("http://", "https://")) or token.startswith("@user")


def simhash64(normalized: str) -> int:
    """64-bit SimHash as a SIGNED integer (fits PostgreSQL BIGINT).

    Features: word tokens (split on whitespace, punctuation stripped at both ends)
    and word bigrams, each weight 1. URLs and ``@user`` are NOT features: a copy that
    only adds a link or a mention ("retweet with a minor addition") keeps the same
    SimHash (the exact hash still differs). Feature hash = ``blake2b(feature.encode(),
    digest_size=8)`` read big-endian. Empty text → 0. Unsigned results ≥ 2**63 are
    converted with ``value - 2**64``.
    """
    tokens = [
        t
        for t in (_EDGE_PUNCT.sub("", raw) for raw in normalized.split() if not _is_noise(raw))
        if t
    ]
    if not tokens:
        return 0
    features = tokens + [f"{a} {b}" for a, b in itertools.pairwise(tokens)]
    counts = [0] * 64
    for feature in features:
        h = int.from_bytes(hashlib.blake2b(feature.encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            counts[bit] += 1 if (h >> bit) & 1 else -1
    value = 0
    for bit in range(64):
        if counts[bit] > 0:
            value |= 1 << bit
    return value - (1 << 64) if value >= 1 << 63 else value


def simhash_bands(value: int) -> tuple[int, int, int, int]:
    """Four 16-bit bands, ``band[i] = (value >> (16 * i)) & 0xFFFF`` (works for the signed
    value; matches the generated columns ``simhash_b0..b3`` of ``content_items``)."""
    return (
        value & 0xFFFF,
        (value >> 16) & 0xFFFF,
        (value >> 32) & 0xFFFF,
        (value >> 48) & 0xFFFF,
    )


def hamming(a: int, b: int) -> int:
    """Hamming distance between two 64-bit values (signed or unsigned)."""
    return ((a ^ b) & 0xFFFFFFFFFFFFFFFF).bit_count()
