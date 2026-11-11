from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class Embedder(Protocol):
    """Text embeddings through an API (no local model: RAM is billed)."""

    model: str
    version: str

    async def embed(self, texts: Sequence[str], *, dimensions: int) -> list[list[float]]: ...
