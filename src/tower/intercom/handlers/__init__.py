"""Slash command logic, independent of discord.py: ``(gate, acting, locale, args) -> Reply``.

A ``Reply`` is a **card** described as pure data (title with its icon, accent, body,
quoted items, footer, controls and action buttons). ``tower.intercom.ui.render`` turns it
into a Components V2 ``LayoutView`` (see ``docs/design/DESIGN.md``). Handlers never decide
permissions: gate does. A ``GateError`` becomes a calm error card with the ``TWR-*`` code.

Every interactive component of a reply carries a ``custom_id`` that matches a registered
``DynamicItem`` template (persistent: clicks keep working after a restart); the test
``test_intercom_ui`` checks it.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Concatenate, Literal

from tower.intercom.gate_client import GateClient, GateError, GateUnavailable
from tower.intercom.i18n import has, t
from tower.intercom.ui import emojis

__all__ = [
    "MAX_ITEMS",
    "MAX_MESSAGE",
    "Accent",
    "Button",
    "Option",
    "Reply",
    "Select",
    "card",
    "code",
    "error_card",
    "error_reply",
    "fmt_dt",
    "fmt_usd",
    "guarded",
    "listing",
    "ok",
    "short",
    "success",
]

MAX_MESSAGE = 3800
"""Components V2 limit all text displays of a message to 4000 characters; keep a margin."""
MAX_ITEMS = 20
"""Quoted items shown in one card (one text display each; 40 components per message)."""

Accent = Literal["primary", "success", "warning", "error", "critical", "neutral", "developer"]
ButtonStyle = Literal["primary", "secondary", "success", "danger", "link"]


@dataclass(frozen=True, slots=True)
class Button:
    """A button. ``custom_id`` must match a registered ``DynamicItem`` template, except for
    a link button (``url`` set, ``style="link"``)."""

    label: str
    custom_id: str = ""
    icon: str = ""
    style: ButtonStyle = "secondary"
    disabled: bool = False
    url: str | None = None


@dataclass(frozen=True, slots=True)
class Option:
    value: str
    label: str
    description: str = ""
    icon: str = ""
    default: bool = False


@dataclass(frozen=True, slots=True)
class Select:
    """A select. ``kind="string"`` uses ``options``; ``kind="user"`` is a user picker."""

    custom_id: str
    placeholder: str = ""
    options: tuple[Option, ...] = ()
    min_values: int = 1
    max_values: int = 1
    kind: Literal["string", "user"] = "string"
    disabled: bool = False


@dataclass(frozen=True, slots=True)
class Field:
    """A labelled control inside the card: ``**label**`` + ``-# help`` + the control."""

    label: str
    control: Select | tuple[Button, ...]
    help: str = ""
    required: bool = False


@dataclass(frozen=True, slots=True)
class Reply:
    """A card (see ``docs/design/DESIGN.md`` §2). ``text`` is a plain rendering kept for
    logs and tests."""

    body: str = ""
    title: str = ""
    icon: str = ""
    accent: Accent = "primary"
    details: tuple[tuple[str, str], ...] = ()
    """``**label** · value`` lines (wrap dynamic values with ``code()``)."""
    items: tuple[str, ...] = ()
    """Quoted list elements (one quote block each, no separator between them)."""
    more: str = ""
    """Grey line after the items (``... and 4 more``)."""
    fields: tuple[Field, ...] = ()
    footer: str = ""
    breadcrumb: tuple[str, ...] = ()
    sections: tuple[str, ...] = ()
    """Text blocks after the body, each one under a separator (forum starter)."""
    buttons: tuple[Button, ...] = ()
    """Action bar, outside the container."""
    menu: Select | None = None
    """A select on its own row under the action bar (forum "More actions")."""
    ephemeral: bool = True
    final: bool = False
    """The action settled the item (e.g. draft validated): the app may drop its buttons."""
    extra: tuple[Reply, ...] = field(default=())
    """Further cards sent as separate messages after this one (long drafts)."""

    @property
    def text(self) -> str:
        parts: list[str] = []
        if self.breadcrumb:
            parts.append(" > ".join(self.breadcrumb))
        if self.title:
            parts.append(self.title)
        if self.body:
            parts.append(self.body)
        parts += list(self.sections)
        parts += [f"{k}: {v}" for k, v in self.details]
        parts += list(self.items)
        if self.more:
            parts.append(self.more)
        for f in self.fields:
            parts.append(f.label)
        if self.footer:
            parts.append(self.footer)
        parts += [b.label for b in self.buttons]
        if self.menu is not None:
            parts += [o.label for o in self.menu.options]
        return "\n".join(parts)


def card(
    title: str,
    body: str = "",
    *,
    icon: str = "",
    accent: Accent = "primary",
    **kwargs: object,
) -> Reply:
    return Reply(body=body, title=title, icon=icon, accent=accent, **kwargs)  # type: ignore[arg-type]


def ok(body: str, *, title: str = "", icon: str = emojis.INFO, **kwargs: object) -> Reply:
    """Informative card (blue)."""
    return card(title, body, icon=icon if title else "", accent="primary", **kwargs)


def success(title: str, body: str = "", **kwargs: object) -> Reply:
    """Done / saved card (green: only for a success)."""
    return card(title, body, icon=emojis.DONE, accent="success", **kwargs)


def error_card(title: str, body: str = "", *, code_: str = "", severity: str = "error") -> Reply:
    icon = {"info": emojis.INFO, "warning": emojis.WARNING}.get(severity, emojis.ERROR)
    accent: Accent = {"info": "primary", "warning": "warning"}.get(severity, "error")  # type: ignore[assignment]
    return card(title, body, icon=icon, accent=accent, footer=f"`{code_}`" if code_ else "")


_WARNING_CODES = frozenset({"TWR-AUTH-005", "TWR-GEN-003", "TWR-GEN-004"})


def error_reply(locale: str, exc: GateError) -> Reply:
    """Calm error card for a gate error; always shows the ``TWR-*`` code."""
    key = f"error.{exc.code}"
    if isinstance(exc, GateUnavailable):
        key = "error.TWR-BOT-001"
    text = (
        t(locale, key, code=exc.code, message=exc.message)
        if has(locale, key)
        else t(locale, "error.generic", code=exc.code, message=exc.message)
    )
    # The catalogue messages end with "(TWR-…)": the code moves to the footer.
    text = text.replace(f" ({exc.code})", "").replace(f"({exc.code})", "").strip()
    title_key = f"error.title.{exc.code}"
    title = t(locale, title_key) if has(locale, title_key) else t(locale, "error.title")
    severity = "warning" if exc.code in _WARNING_CODES or exc.status in (400, 404, 409) else "error"
    return error_card(title, text, code_=exc.code, severity=severity)


def guarded[**P](
    fn: Callable[Concatenate[GateClient, int, str, P], Awaitable[Reply]],
) -> Callable[Concatenate[GateClient, int, str, P], Awaitable[Reply]]:
    """Turn any ``GateError`` (including ``GateUnavailable``) into an error card."""

    @functools.wraps(fn)
    async def wrapper(
        gate: GateClient, acting: int, locale: str, /, *args: P.args, **kwargs: P.kwargs
    ) -> Reply:
        try:
            return await fn(gate, acting, locale, *args, **kwargs)
        except GateError as exc:
            return error_reply(locale, exc)

    return wrapper


def code(value: object) -> str:
    """Dynamic value in backticks, neutralising backticks typed by a user."""
    return f"`{str(value).replace('`', 'ˋ')}`"


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


def listing(
    locale: str, lines: Sequence[str], *, limit: int = MAX_ITEMS
) -> tuple[tuple[str, ...], str]:
    """Items for a card list plus the ``more`` line, cut to fit one V2 message."""
    out: list[str] = []
    size = 0
    for i, line in enumerate(lines):
        if i >= limit or size + len(line) > MAX_MESSAGE - 600:
            return tuple(out), t(locale, "common.more", count=len(lines) - i)
        out.append(line)
        size += len(line) + 4
    return tuple(out), ""
