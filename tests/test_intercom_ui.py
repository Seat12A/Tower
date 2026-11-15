"""Components V2 renderer and persistent components of intercom (no Discord connection)."""

from __future__ import annotations

import importlib

import pytest
from discord import ui

from tests.intercom_ui import templates
from tower.intercom import commands
from tower.intercom.handlers import Button, Field, Option, Reply, Select, error_card, listing
from tower.intercom.ui import emojis, render


def test_layout_anatomy() -> None:
    reply = Reply(
        title="Title",
        icon=emojis.INFO,
        body="Body",
        breadcrumb=("Team", "Member"),
        details=(("Label", "`v`"),),
        items=("one", "two\nlines"),
        more="... and 3 more",
        fields=(
            Field(
                "Field",
                Select("t12a:x:y", options=(Option("a", "A", default=True),)),
                help="help",
                required=True,
            ),
        ),
        footer="footer",
        buttons=(Button("Go", "t12a:x:go", icon=emojis.NEXT),),
    )
    view = render.layout(reply)
    assert view.timeout is None
    texts = [i.content for i in view.walk_children() if isinstance(i, ui.TextDisplay)]
    assert texts[0] == "-# Team › Member"
    assert texts[1] == f"### {emojis.INFO} Title"
    assert "> two\n> lines" in texts
    assert f"**Field**{emojis.REQUIRED_FIELDS}\n-# help" in texts
    assert texts[-1] == "-# footer"
    top = list(view.children)
    assert isinstance(top[0], ui.Container) and isinstance(top[1], ui.ActionRow)


def test_layout_respects_v2_text_budget() -> None:
    reply = Reply(title="T", body="x" * 5000, items=tuple("y" * 400 for _ in range(30)))
    view = render.layout(reply)
    total = sum(len(i.content) for i in view.walk_children() if isinstance(i, ui.TextDisplay))
    assert total <= 4000
    assert sum(1 for _ in view.walk_children()) <= 40


def test_listing_cuts_with_more_line() -> None:
    items, more = listing("en", [f"line {i}" for i in range(30)])
    assert len(items) == 20 and "10" in more


def test_error_card_is_calm() -> None:
    reply = error_card("Not found", "Check the id.", code_="TWR-GEN-002", severity="warning")
    assert reply.accent == "warning" and "TWR-GEN-002" in reply.footer


def test_every_module_registers() -> None:
    for name in commands.MODULES:
        module = importlib.import_module(f"tower.intercom.commands.{name}")
        assert callable(module.register)


def test_dynamic_item_templates_are_namespaced_and_disjoint() -> None:
    samples: list[str] = []
    for name in commands.MODULES:
        module = importlib.import_module(f"tower.intercom.commands.{name}")
        samples += list(getattr(module, "SAMPLE_CUSTOM_IDS", ()))
    assert samples, "each module lists one sample custom id per dynamic item"
    for cid in samples:
        assert cid.startswith(commands.CUSTOM_ID_PREFIX) or cid.startswith("fc:"), cid
        hits = [p.pattern for p in templates() if p.fullmatch(cid)]
        assert len(hits) == 1, (cid, hits)
    assert len(samples) >= len(templates())


@pytest.mark.parametrize("name", commands.MODULES)
def test_module_imports_without_side_effects(name: str) -> None:
    importlib.import_module(f"tower.intercom.commands.{name}")
