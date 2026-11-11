"""Text normalisation and fingerprints (E0). Standard library only.

The original text is always stored unchanged; these functions only feed the
fingerprints, the language detection and the useful word count.
"""

from __future__ import annotations

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

SIMHASH_BANDS = 4
SIMHASH_BAND_BITS = 16


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
    raise NotImplementedError


def useful_word_count(text: str) -> int:
    """Number of useful words in ``normalize(text)``: tokens with at least two letters
    (``str.isalpha`` on at least two characters), excluding URLs and ``@user``.
    Hashtags count by their word (``#retraites`` → one word). For scripts without
    spaces (CJK, Thai), each run of letters counts one word per 2 characters."""
    raise NotImplementedError


def content_hash(normalized: str) -> bytes:
    """SHA-256 of the UTF-8 normalised text (32 bytes)."""
    raise NotImplementedError


def simhash64(normalized: str) -> int:
    """64-bit SimHash as a SIGNED integer (fits PostgreSQL BIGINT).

    Features: word tokens (split on whitespace, punctuation stripped at both ends)
    and word bigrams, each weight 1; feature hash = ``blake2b(feature.encode(),
    digest_size=8)`` read big-endian. Empty text → 0. Unsigned results ≥ 2**63 are
    converted with ``value - 2**64``.
    """
    raise NotImplementedError


def simhash_bands(value: int) -> tuple[int, int, int, int]:
    """Four 16-bit bands, ``band[i] = (value >> (16 * i)) & 0xFFFF`` (works for the signed
    value; matches the generated columns ``simhash_b0..b3`` of ``content_items``)."""
    raise NotImplementedError


def hamming(a: int, b: int) -> int:
    """Hamming distance between two 64-bit values (signed or unsigned)."""
    raise NotImplementedError
