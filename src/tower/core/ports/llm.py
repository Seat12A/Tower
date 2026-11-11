"""LLM contracts: what callers use (``LLMClient``), what adapters implement
(``LLMProvider``), and batch processing (``BatchLLMClient``, contract only)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel

from tower.core.domain.llm import Level, TokenUsage

__all__ = [
    "BatchLLMClient",
    "BatchStatus",
    "LLMClient",
    "LLMProvider",
    "LLMRequest",
    "LLMResult",
    "ProviderError",
    "ProviderFatal",
    "ProviderInvalidOutput",
    "ProviderRefusal",
    "ProviderResponse",
    "ProviderRetryable",
    "TokenUsage",
]


@dataclass(frozen=True, slots=True)
class LLMRequest:
    role: str
    """``extract``, ``judge``…: selects the route in ``llm_routes``."""
    level: Level
    prompt_id: str
    """The version comes from the prompt registry (hash of the file)."""
    input: Mapping[str, Any]
    """Canonical JSON-serialisable input; untrusted text is delimited by the router."""
    schema: type[BaseModel]
    max_output_tokens: int
    budget_scope: str = "global"
    """Additional budget scope (``level:N3``, ``surge``…); ``global`` always applies."""


@dataclass(frozen=True, slots=True)
class LLMResult:
    output: BaseModel
    provider: str
    model: str
    usage: TokenUsage
    cost_usd_micros: int
    free_tier: bool
    cache_hit: bool
    request_id: str
    prompt_version: str = ""
    route_version: int = 0


class LLMClient(Protocol):
    """Structured model call with routing, cache, budget (implemented by ``LLMRouter``)."""

    async def complete(self, request: LLMRequest) -> LLMResult: ...


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    output_json: dict[str, Any]
    usage: TokenUsage
    request_id: str
    stop_reason: str


class LLMProvider(Protocol):
    """One provider account (an API key). Implemented by the adapters.

    ``messages`` are provider-neutral: ``{"role": "user" | "assistant", "content": str}``.
    ``params`` come from the route (``{"effort": "low"}``…); unknown keys are ignored.
    """

    name: str

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
    ) -> ProviderResponse: ...


class ProviderError(Exception):
    """Base of provider errors. ``usage`` is billed even when the call failed."""

    def __init__(
        self,
        message: str,
        *,
        usage: TokenUsage | None = None,
        request_id: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.usage = usage or TokenUsage()
        self.request_id = request_id
        self.details = dict(details or {})


class ProviderRefusal(ProviderError):
    """The model declined (Anthropic ``stop_reason == "refusal"``, OpenAI refusal part)."""


class ProviderRetryable(ProviderError):
    """Rate limit, timeout, connection error, 5xx: the job will retry later."""

    def __init__(self, message: str, *, retry_after: float | None = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class ProviderInvalidOutput(ProviderError):
    """Output is not valid JSON, truncated (max tokens) or not an object."""


class ProviderFatal(ProviderError):
    """Authentication, permission, bad request: retrying will not help."""


@dataclass(frozen=True, slots=True)
class BatchStatus:
    batch_id: str
    state: str
    """``in_progress``, ``ended``, ``failed``, ``expired``, ``canceled``."""
    results: Mapping[str, ProviderResponse | ProviderError] = field(default_factory=dict)
    """By custom id, once ``state == "ended"``."""


class BatchLLMClient(Protocol):
    """Batch APIs of both providers (-50 %). Contract only: implemented in a later lot."""

    async def submit(
        self, model: str, requests: Mapping[str, Mapping[str, Any]], *, params: Mapping[str, Any]
    ) -> str: ...

    async def poll(self, batch_id: str) -> BatchStatus: ...
