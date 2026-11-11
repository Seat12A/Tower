"""Slash command logic, independent of discord.py: ``(gate, acting, locale, args) -> Reply``.

Handlers never decide permissions: gate does. A ``GateError`` becomes an ephemeral reply
with the ``TWR-*`` code and a translated message.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Reply"]


@dataclass(frozen=True, slots=True)
class Reply:
    text: str
    ephemeral: bool = False
