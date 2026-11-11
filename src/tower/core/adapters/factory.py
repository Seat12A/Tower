"""Builds provider adapters from settings. SDKs are only loaded when a key is configured."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tower.core.config import Settings
    from tower.core.ports.embeddings import Embedder
    from tower.core.ports.llm import LLMProvider

__all__ = ["build_embedder", "build_llm_providers"]


def build_llm_providers(settings: Settings) -> dict[str, LLMProvider]:
    providers: dict[str, LLMProvider] = {}
    if settings.openai_api_key is not None:
        from tower.core.adapters.openai_llm import OpenAIResponsesProvider

        providers["openai"] = OpenAIResponsesProvider(
            settings.openai_api_key.get_secret_value(), name="openai"
        )
    if settings.openai_api_key_shared is not None:
        from tower.core.adapters.openai_llm import OpenAIResponsesProvider

        providers["openai_shared"] = OpenAIResponsesProvider(
            settings.openai_api_key_shared.get_secret_value(), name="openai_shared"
        )
    if settings.anthropic_api_key is not None:
        from tower.core.adapters.anthropic_llm import AnthropicMessagesProvider

        providers["anthropic"] = AnthropicMessagesProvider(
            settings.anthropic_api_key.get_secret_value(), name="anthropic"
        )
    return providers


def build_embedder(settings: Settings) -> Embedder | None:
    key = settings.openai_api_key or settings.openai_api_key_shared
    if key is None:
        return None
    from tower.core.adapters.openai_embedder import OpenAIEmbedder

    return OpenAIEmbedder(key.get_secret_value())
