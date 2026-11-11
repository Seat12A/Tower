"""Datamarking of untrusted text (spotlighting): the words of a post or a passage are
joined by a marker character, so the model can tell data from instructions.

Quotes returned by a model are unmarked before any check: a check always runs on the
original text, never on the marked one.
"""

from __future__ import annotations

__all__ = ["MARKER", "datamark", "unmark"]

MARKER = "ˆ"
"""MODIFIER LETTER CIRCUMFLEX ACCENT (ˆ): rare in real text, one token in most tokenizers."""


def datamark(text: str) -> str:
    """Replace every run of whitespace by ``MARKER`` (line breaks kept as ``MARKER\\n``).
    Existing ``MARKER`` characters in ``text`` are removed first (no forged boundaries)."""
    raise NotImplementedError


def unmark(text: str) -> str:
    """Inverse for model outputs: every ``MARKER`` becomes a space, spaces collapsed."""
    raise NotImplementedError
