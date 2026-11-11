"""OpenAI embeddings adapter. The ``openai`` SDK is imported lazily (RAM)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from tower.core.adapters.openai_llm import raise_openai_error
from tower.core.ports.llm import ProviderError

__all__ = ["OpenAIEmbedder"]


class OpenAIEmbedder:
    def __init__(
        self,
        api_key: str,
        *,
        model: str = "text-embedding-3-small",
        client: Any = None,
        batch_size: int = 256,
        timeout: float = 30.0,
    ) -> None:
        self.model = model
        self.version = model
        self._batch_size = max(1, batch_size)
        if client is None:
            import openai

            client = openai.AsyncOpenAI(api_key=api_key, timeout=timeout)
        self._client: Any = client

    async def embed(self, texts: Sequence[str], *, dimensions: int) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            try:
                resp = await self._client.embeddings.create(
                    model=self.model, input=list(batch), dimensions=dimensions
                )
            except ProviderError:
                raise
            except Exception as exc:
                raise_openai_error(exc)
            ordered = sorted(resp.data, key=lambda item: item.index)
            vectors.extend([list(item.embedding) for item in ordered])
        return vectors
