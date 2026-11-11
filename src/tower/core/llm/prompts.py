"""Versioned prompt registry: ``<prompt_id>.md`` files, version = SHA-256 prefix."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from tower.core.llm.errors import UnknownPrompt

__all__ = ["Prompt", "PromptRegistry"]

VERSION_LENGTH = 12


@dataclass(frozen=True, slots=True)
class Prompt:
    id: str
    text: str
    version: str


class PromptRegistry:
    """Loads ``*.md`` files from the given directories on first use (id = file stem)."""

    def __init__(self, dirs: Sequence[Path]) -> None:
        self._dirs = tuple(dirs)
        self._prompts: dict[str, Prompt] | None = None

    @classmethod
    def default(cls) -> PromptRegistry:
        """All ``src/tower/services/*/prompts/`` directories (files only, no service import)."""
        import tower.services

        root = Path(tower.services.__file__).parent
        return cls(sorted(p for p in root.glob("*/prompts") if p.is_dir()))

    def _load(self) -> dict[str, Prompt]:
        prompts: dict[str, Prompt] = {}
        sources: dict[str, Path] = {}
        for directory in self._dirs:
            for path in sorted(directory.glob("*.md")):
                if path.stem in prompts:
                    raise ValueError(
                        f"Duplicate prompt id {path.stem!r}: {sources[path.stem]} and {path}"
                    )
                raw = path.read_bytes()
                version = hashlib.sha256(raw).hexdigest()[:VERSION_LENGTH]
                prompts[path.stem] = Prompt(path.stem, raw.decode("utf-8"), version)
                sources[path.stem] = path
        return prompts

    def get(self, prompt_id: str) -> Prompt:
        if self._prompts is None:
            self._prompts = self._load()
        try:
            return self._prompts[prompt_id]
        except KeyError:
            raise UnknownPrompt(f"Unknown prompt id: {prompt_id}", prompt_id=prompt_id) from None
