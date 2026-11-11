"""Ports: interfaces for external dependencies, implemented by adapters.

Contracts only (no heavy import). Shared adapters live in ``tower.core.adapters`` and
import their SDKs lazily; tests replace them with doubles.
"""

from tower.core.ports.clock import Clock, SystemClock
from tower.core.ports.embeddings import Embedder
from tower.core.ports.external import Notifier, XClient, XPost
from tower.core.ports.knowledge import Evidence, KnowledgeSource
from tower.core.ports.llm import (
    BatchLLMClient,
    LLMClient,
    LLMProvider,
    LLMRequest,
    LLMResult,
    ProviderResponse,
)
from tower.core.ports.platforms import ContentItem, PlatformClient
from tower.core.ports.web import FetchedPage, SearchClient, SearchResult, WebFetcher

__all__ = [
    "BatchLLMClient",
    "Clock",
    "ContentItem",
    "Embedder",
    "Evidence",
    "FetchedPage",
    "KnowledgeSource",
    "LLMClient",
    "LLMProvider",
    "LLMRequest",
    "LLMResult",
    "Notifier",
    "PlatformClient",
    "ProviderResponse",
    "SearchClient",
    "SearchResult",
    "SystemClock",
    "WebFetcher",
    "XClient",
    "XPost",
]
