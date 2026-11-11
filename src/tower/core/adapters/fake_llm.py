"""Fake model provider (``TOWER_LLM_FAKE=1``): schema-valid outputs per role, for
development, smoke tests and the evaluation demo. Never used in production."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from tower.core.ports.llm import ProviderResponse

__all__ = ["FakeLLMProvider"]


class FakeLLMProvider:
    """``LLMProvider`` producing deterministic outputs from the input (by schema name)."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: Sequence[Mapping[str, str]],
        json_schema: Mapping[str, Any],
        schema_name: str,
        max_output_tokens: int,
        params: Mapping[str, Any],
    ) -> ProviderResponse:
        raise NotImplementedError
