"""Helpers for intercom tests: every component of a reply must be persistent."""

from __future__ import annotations

import re
from functools import cache

from tower.intercom.handlers import Button, Reply, Select


@cache
def templates() -> tuple[re.Pattern[str], ...]:
    from tower.intercom import commands
    from tower.intercom.gate_client import GateClient

    ctx = commands.Context(gate=GateClient("http://gate.invalid", "t"), default_locale="en")
    return tuple(cls.__discord_ui_compiled_template__ for cls in commands.dynamic_items(ctx))


def custom_ids(reply: Reply) -> list[str]:
    out: list[str] = []
    controls: list[Button | Select] = list(reply.buttons)
    for f in reply.fields:
        controls += [f.control] if isinstance(f.control, Select) else list(f.control)
    for c in controls:
        if isinstance(c, Button) and c.url:
            continue
        out.append(c.custom_id)
    for more in reply.extra:
        out += custom_ids(more)
    return out


def assert_persistent(reply: Reply) -> None:
    """Each custom id is at most 100 characters and matches exactly one template."""
    for cid in custom_ids(reply):
        assert len(cid) <= 100, cid
        hits = [p for p in templates() if p.fullmatch(cid)]
        assert len(hits) == 1, f"{cid!r} matches {len(hits)} dynamic item templates"
