"""Slash command logic, independent of discord.py: ``(gate, acting, locale, args) -> Reply``.

Handlers never decide permissions: gate does. A ``GateError`` becomes an ephemeral reply
with the ``TWR-*`` code and a translated message. Every reply is ephemeral: the bot is an
internal tool and its answers stay private to the member who ran the command.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Concatenate

from tower.intercom.gate_client import GateClient, GateError, GateUnavailable
from tower.intercom.i18n import has, t

__all__ = [
    "MAX_MESSAGE",
    "Reply",
    "error_reply",
    "fmt_dt",
    "fmt_usd",
    "guarded",
    "listing",
    "ok",
    "short",
]

MAX_MESSAGE = 1900
"""Discord limits a message to 2000 characters; keep a margin."""


@dataclass(frozen=True, slots=True)
class Reply:
    text: str
    ephemeral: bool = False


def ok(text: str) -> Reply:
    """Successful reply (private, like every reply of the bot)."""
    return Reply(text, ephemeral=True)


def error_reply(locale: str, exc: GateError) -> Reply:
    """Ephemeral reply for a gate error; always shows the ``TWR-*`` code."""
    key = f"error.{exc.code}"
    if isinstance(exc, GateUnavailable):
        key = "error.TWR-BOT-001"
    text = (
        t(locale, key, code=exc.code, message=exc.message)
        if has(locale, key)
        else t(locale, "error.generic", code=exc.code, message=exc.message)
    )
    return Reply(text, ephemeral=True)


def guarded[**P](
    fn: Callable[Concatenate[GateClient, int, str, P], Awaitable[Reply]],
) -> Callable[Concatenate[GateClient, int, str, P], Awaitable[Reply]]:
    """Turn any ``GateError`` (including ``GateUnavailable``) into an error reply."""

    @functools.wraps(fn)
    async def wrapper(
        gate: GateClient, acting: int, locale: str, /, *args: P.args, **kwargs: P.kwargs
    ) -> Reply:
        try:
            return await fn(gate, acting, locale, *args, **kwargs)
        except GateError as exc:
            return error_reply(locale, exc)

    return wrapper


def short(identifier: object, length: int = 8) -> str:
    return str(identifier)[:length]


def fmt_dt(value: object) -> str:
    """ISO timestamp to ``YYYY-MM-DD HH:MM`` (UTC as served by gate); unknown: as is."""
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(str(value)).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(value)


def fmt_usd(micros: object) -> str:
    """USD micros to a 2-decimal amount (``1500000`` -> ``1.50``)."""
    cents, remainder = divmod(int(str(micros)), 10_000)
    cents += 1 if remainder >= 5_000 else 0
    return f"{cents // 100}.{cents % 100:02d}"


def listing(locale: str, header: str, lines: Sequence[str]) -> str:
    """Header plus lines, cut to fit one Discord message (with a ``more`` marker)."""
    out = [header]
    size = len(header)
    for i, line in enumerate(lines):
        footer = t(locale, "common.more", count=len(lines) - i)
        if size + len(line) + 1 + len(footer) + 1 > MAX_MESSAGE:
            out.append(footer)
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)
