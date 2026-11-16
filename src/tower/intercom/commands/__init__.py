"""Slash commands and persistent components of intercom, one module per area.

Each module exposes ``register(tree, ctx)`` (adds its commands) and ``DYNAMIC_ITEMS``
(its ``DynamicItem`` classes, registered at start-up so every button and select keeps
working after a restart). The modules import discord: only the bot process loads them.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any

from tower.intercom.gate_client import GateClient

__all__ = ["MODULES", "Context", "dynamic_items", "register_all"]

MODULES = (
    "help",
    "team",
    "permissions",
    "watchlist",
    "cases",
    "incidents",
    "usage",
    "profiles",
    "registry",
    "badges",
    "factcheck",
    "narratives",
    "drafts",
)
"""Order of registration (also the order of the sections of ``/help``)."""

CUSTOM_ID_PREFIX = "t12a:"
"""Every custom id of the bot starts with it (old draft buttons keep ``fc:``)."""


@dataclass(frozen=True, slots=True)
class Context:
    gate: GateClient
    default_locale: str
    """Locale of the public messages (notifications, posted drafts)."""


def _modules() -> list[Any]:
    return [importlib.import_module(f"tower.intercom.commands.{name}") for name in MODULES]


def register_all(tree: Any, ctx: Context) -> None:
    for module in _modules():
        module.register(tree, ctx)


def dynamic_items(ctx: Context) -> list[type]:
    """Every ``DynamicItem`` class of the bot. A module may build its classes around the
    context: it then exposes ``dynamic_items(ctx)`` instead of ``DYNAMIC_ITEMS``."""
    items: list[type] = []
    for module in _modules():
        factory = getattr(module, "dynamic_items", None)
        items += factory(ctx) if factory else list(getattr(module, "DYNAMIC_ITEMS", ()))
    return items
