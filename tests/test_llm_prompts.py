from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tower.core.llm.errors import UnknownPrompt
from tower.core.llm.prompts import PromptRegistry


def test_version_is_sha256_prefix_of_file_bytes(tmp_path: Path) -> None:
    (tmp_path / "extract.md").write_bytes(b"You extract claims.\n")
    prompt = PromptRegistry([tmp_path]).get("extract")
    assert prompt.id == "extract"
    assert prompt.text == "You extract claims.\n"
    assert prompt.version == hashlib.sha256(b"You extract claims.\n").hexdigest()[:12]
    assert len(prompt.version) == 12


def test_version_changes_with_content(tmp_path: Path) -> None:
    path = tmp_path / "judge.md"
    path.write_text("v1")
    first = PromptRegistry([tmp_path]).get("judge").version
    path.write_text("v2")
    assert PromptRegistry([tmp_path]).get("judge").version != first


def test_only_markdown_files_are_loaded(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    registry = PromptRegistry([tmp_path])
    registry.get("a")
    with pytest.raises(UnknownPrompt):
        registry.get("b")


def test_duplicate_id_across_dirs_fails_at_load(tmp_path: Path) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    (one / "same.md").write_text("1")
    (two / "same.md").write_text("2")
    registry = PromptRegistry([one, two])  # lazy: no error yet
    with pytest.raises(ValueError, match="Duplicate prompt id"):
        registry.get("same")


def test_unknown_prompt_has_catalog_code(tmp_path: Path) -> None:
    with pytest.raises(UnknownPrompt) as info:
        PromptRegistry([tmp_path]).get("missing")
    assert info.value.code.code == "TWR-LLM-003"
    assert info.value.details["prompt_id"] == "missing"


def test_missing_directory_is_ignored(tmp_path: Path) -> None:
    with pytest.raises(UnknownPrompt):
        PromptRegistry([tmp_path / "nope"]).get("x")


def test_default_registry_has_no_duplicate() -> None:
    registry = PromptRegistry.default()
    with pytest.raises(UnknownPrompt):
        registry.get("definitely-not-a-prompt")  # forces the load: raises on duplicates
