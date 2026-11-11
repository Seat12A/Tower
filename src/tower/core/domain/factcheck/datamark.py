"""Datamarking of untrusted text (spotlighting): the words of a post or a passage are
joined by a marker character, so the model can tell data from instructions.

Quotes returned by a model are unmarked before any check: a check always runs on the
original text, never on the marked one.
"""

from __future__ import annotations

import re

__all__ = ["MARKER", "datamark", "unmark"]

MARKER = "ˆ"
"""MODIFIER LETTER CIRCUMFLEX ACCENT (ˆ): rare in real text, one token in most tokenizers."""


def datamark(text: str) -> str:
    """Replace every run of whitespace by ``MARKER`` (line breaks kept as ``MARKER\\n``).
    Existing ``MARKER`` characters in ``text`` are removed first (no forged boundaries)."""
    cleaned = text.replace(MARKER, "")
    return _WS_RUN.sub(_mark_run, cleaned)


def unmark(text: str) -> str:
    """Inverse for model outputs: every ``MARKER`` becomes a space, spaces collapsed."""
    spaced = text.replace(MARKER, " ")
    spaced = _BLANKS.sub(" ", spaced)
    spaced = _AROUND_NEWLINE.sub("\n", spaced)
    return spaced.strip()


_WS_RUN = re.compile(r"\s+")
_BLANKS = re.compile(r"[^\S\n]+")
_AROUND_NEWLINE = re.compile(r" *\n[ \n]*")


def _mark_run(match: re.Match[str]) -> str:
    run = match.group(0)
    return MARKER + "\n" if "\n" in run or "\r" in run else MARKER
