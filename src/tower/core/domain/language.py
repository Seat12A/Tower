"""Lightweight language detection, without any model. Standard library only."""

from __future__ import annotations

UNDETERMINED = "und"
NON_LINGUISTIC_HINTS: frozenset[str] = frozenset({"und", "zxx", "qme", "qht", "qam", "qct", "qst"})
"""Platform hints that carry no language (X uses ``qme`` for media only, etc.)."""


def normalize_hint(hint: str | None) -> str | None:
    """Primary subtag, lowercase, if ``hint`` looks like BCP 47
    (``^[A-Za-z]{2,3}([-_][A-Za-z0-9]{1,8})*$``) and is not in ``NON_LINGUISTIC_HINTS``;
    else None. ``"pt-BR"`` → ``"pt"``."""
    raise NotImplementedError


def detect_language(text: str, hint: str | None = None) -> tuple[str, float]:
    """Return ``(code, confidence)`` with ``code`` a BCP 47 primary subtag or ``"und"``.

    1. A valid platform hint wins: ``(normalize_hint(hint), 1.0)``.
    2. Script: if ≥ 60 % of letters belong to one non-Latin script, return its language
       when unambiguous (Hebrew ``he``, Greek ``el``, Thai ``th``, Hangul ``ko``, kana
       ``ja``, Georgian ``ka``, Armenian ``hy``) or the dominant language of the script
       with a lower confidence (Arabic ``ar``, Cyrillic ``ru``, Devanagari ``hi``, Han
       only ``zh``) — confidence 0.9 for unambiguous scripts, 0.6 otherwise.
    3. Latin script: score function words (stopword lists) for fr, en, es, de, it, pt,
       nl; best language wins if it has ≥ 2 hits and leads the second by a clear margin;
       confidence in [0.5, 0.95] growing with hits and margin.
    4. Otherwise (too short, mixed, no letters) → ``("und", 0.0)``.
    """
    raise NotImplementedError
