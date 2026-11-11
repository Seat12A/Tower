"""E2 fidelity checks, in both directions (pipeline.md E2).

- Contamination: every number and every named entity of a claim must appear in its post.
- Omission: a post containing numbers must yield at least one claim carrying a number
  (or an absence reason other than ``no_claim``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "FidelityIssue",
    "check_claim_fidelity",
    "check_omission",
    "extract_numbers",
    "missing_entities",
]


@dataclass(frozen=True, slots=True)
class FidelityIssue:
    kind: str
    """``number`` or ``entity`` (contamination), ``omission``."""
    value: str


def extract_numbers(text: str) -> frozenset[str]:
    """Canonical numbers of ``text``: digits only, thousands separators removed
    (``10 000``, ``10,000``, ``10.000`` → ``10000``), decimal comma or point → ``.``
    (``7,5`` → ``7.5``), trailing zeros of decimals dropped (``7.50`` → ``7.5``),
    percent and currency signs ignored. Years are numbers like any other."""
    raise NotImplementedError


def missing_entities(entities: Sequence[str], post_text: str) -> list[str]:
    """Entities absent from the post, compared casefolded and accent-insensitive, as
    whole words. An entity is present when all its significant words (≥ 3 letters) are,
    or when it is an acronym of capitalized words of the post."""
    raise NotImplementedError


def check_claim_fidelity(
    claim_numbers_text: str, entities: Sequence[str], post_text: str, *, context_text: str = ""
) -> list[FidelityIssue]:
    """Contamination issues of one claim. ``claim_numbers_text`` is the original-language
    claim text plus its signature value fields; numbers are compared canonically.
    ``context_text`` (parent or quoted post) also counts as present."""
    raise NotImplementedError


def check_omission(post_text: str, claims_texts: Sequence[str], *, has_absence: bool) -> bool:
    """True when the post has numbers (other than in URLs and mentions) but no claim
    carries any of them and no absence reason was given: the post must be replayed."""
    raise NotImplementedError
