"""E2 fidelity checks, in both directions (pipeline.md E2).

- Contamination: every number and every named entity of a claim must appear in its post.
- Omission: a post containing numbers must yield at least one claim carrying a number
  (or an absence reason other than ``no_claim``).
"""

from __future__ import annotations

import re
import unicodedata
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
    cleaned = _MENTION.sub(" ", _URL.sub(" ", text))
    cleaned = _SPACE_THOUSANDS.sub(lambda m: m.group(1) + re.sub(r"\D", "", m.group(2)), cleaned)
    numbers: set[str] = set()
    for match in _NUMBER.finditer(cleaned):
        numbers.update(_canonical(match.group(0)))
    return frozenset(numbers)


def missing_entities(entities: Sequence[str], post_text: str) -> list[str]:
    """Entities absent from the post, compared casefolded and accent-insensitive, as
    whole words. An entity is present when all its significant words (≥ 3 letters) are,
    or when it is an acronym of capitalized words of the post."""
    words = set(_words(_fold(post_text)))
    capitalized_runs = _capitalized_runs(post_text)
    missing: list[str] = []
    for entity in entities:
        stripped = entity.strip()
        if not stripped:
            continue
        if _is_acronym(stripped):
            acronym = _fold(stripped.replace(".", ""))
            if acronym in words or _initials_match(acronym, capitalized_runs):
                continue
            missing.append(entity)
            continue
        tokens = _words(_fold(stripped))
        significant = [t for t in tokens if len(t) >= 3] or tokens
        if all(t in words for t in significant):
            continue
        missing.append(entity)
    return missing


def check_claim_fidelity(
    claim_numbers_text: str, entities: Sequence[str], post_text: str, *, context_text: str = ""
) -> list[FidelityIssue]:
    """Contamination issues of one claim. ``claim_numbers_text`` is the original-language
    claim text plus its signature value fields; numbers are compared canonically.
    ``context_text`` (parent or quoted post) also counts as present."""
    available = extract_numbers(post_text) | extract_numbers(context_text)
    issues = [
        FidelityIssue("number", n)
        for n in sorted(extract_numbers(claim_numbers_text), key=lambda v: (float(v), v))
        if n not in available
    ]
    issues.extend(
        FidelityIssue("entity", e)
        for e in missing_entities(entities, f"{post_text}\n{context_text}")
    )
    return issues


def check_omission(post_text: str, claims_texts: Sequence[str], *, has_absence: bool) -> bool:
    """True when the post has numbers (other than in URLs and mentions) but no claim
    carries any of them and no absence reason was given: the post must be replayed."""
    if has_absence:
        return False
    post_numbers = extract_numbers(post_text)
    if not post_numbers:
        return False
    return not any(post_numbers & extract_numbers(claim) for claim in claims_texts)


# Number extraction is ambiguous by nature: "1,000" and "10.000" are thousands in English
# and French respectively but "1,5" and "1.5" are decimals. Rule used (symmetric for post
# and claim, so only consistency matters): a separator followed by exactly three digits
# is a thousands separator ("1,000" -> 1000, "1,234,567" -> 1234567, "10.000" -> 10000)
# unless the integer part starts with 0 ("0,500" -> 0.5); one or two digits, or four and
# more, make it a decimal separator ("7,5" -> 7.5, "1,2345" -> 1.2345). A space (or NBSP)
# between a group of 1-3 digits and exactly three digits is a thousands separator too.
_URL = re.compile(r"https?://\S+")
_MENTION = re.compile(r"(?<![\w@])@\w+")
_SPACE_THOUSANDS = re.compile(r"(?<![0-9])([0-9]{1,3})((?:[ \u00a0\u202f][0-9]{3}(?![0-9]))+)")
_NUMBER = re.compile(r"[0-9]+(?:[.,][0-9]+)*")
_WORD = re.compile(r"[^\W_]+")


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _words(text: str) -> list[str]:
    return _WORD.findall(text)


def _plain(integer: str, fraction: str = "") -> str:
    integer = integer.lstrip("0") or "0"
    fraction = fraction.rstrip("0")
    return f"{integer}.{fraction}" if fraction else integer


def _canonical(token: str) -> list[str]:
    """Canonical number(s) of one raw token (``12.05.2024`` gives three numbers)."""
    seps = re.findall(r"[.,]", token)
    groups = re.split(r"[.,]", token)
    if not seps:
        return [_plain(token)]
    thousands_like = all(len(g) == 3 for g in groups[1:]) and not groups[0].startswith("0")
    if len(set(seps)) == 1:
        if len(seps) == 1:
            if thousands_like:
                return [_plain("".join(groups))]
            return [_plain(groups[0], groups[1])]
        if thousands_like:
            return [_plain("".join(groups))]
        return [_plain(g) for g in groups]  # dates, lists: "12.05.2024", "1,2,3"
    # Mixed separators: the last one is the decimal mark ("1.234,56", "1,234.56").
    head, decimal = token[: token.rfind(seps[-1])], groups[-1]
    head_groups = re.split(r"[.,]", head)
    if (
        len(set(seps[:-1])) == 1
        and seps[-1] != seps[0]
        and all(len(g) == 3 for g in head_groups[1:])
    ):
        return [_plain("".join(head_groups), decimal)]
    return [_plain(g) for g in groups]


def _is_acronym(entity: str) -> bool:
    letters = entity.replace(".", "")
    return (
        " " not in entity and len(letters) >= 2 and letters.isalpha() and letters == letters.upper()
    )


def _capitalized_runs(text: str) -> list[list[str]]:
    """Runs of consecutive capitalized words (separated by whitespace or hyphens only);
    each word is reduced to its folded initial."""
    runs: list[list[str]] = []
    current: list[str] = []
    last_end = -1
    for m in re.finditer(r"[^\W_]+", text):
        word = m.group(0)
        adjacent = current and re.fullmatch(r"[\s-]+", text[last_end : m.start()]) is not None
        if word[0].isupper():
            if current and not adjacent:
                runs.append(current)
                current = []
            current.append(_fold(word[0]))
        else:
            if current:
                runs.append(current)
                current = []
        last_end = m.end()
    if current:
        runs.append(current)
    return runs


def _initials_match(acronym: str, runs: list[list[str]]) -> bool:
    n = len(acronym)
    return any("".join(run[i : i + n]) == acronym for run in runs for i in range(len(run) - n + 1))
