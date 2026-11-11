"""OpenAI Responses API adapter. The ``openai`` SDK is imported lazily (RAM)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, NoReturn

from tower.core.domain.llm import TokenUsage
from tower.core.ports.llm import (
    ProviderError,
    ProviderFatal,
    ProviderInvalidOutput,
    ProviderRefusal,
    ProviderResponse,
    ProviderRetryable,
)

__all__ = ["OpenAIResponsesProvider", "parse_retry_after", "raise_openai_error"]

_RETRYABLE_STATUS = frozenset({408, 409, 429})


def parse_retry_after(headers: Any) -> float | None:
    """Seconds from a ``retry-after`` header, if it is a non-negative number."""
    try:
        raw = headers.get("retry-after")
        if raw is None:
            return None
        value = float(raw)
    except (AttributeError, TypeError, ValueError):
        return None
    return value if value >= 0 else None


def raise_openai_error(exc: Exception, *, request_id: str = "") -> NoReturn:
    """Translate an ``openai`` SDK exception into a provider error (re-raises others)."""
    import openai

    if isinstance(exc, openai.APIStatusError):
        status = exc.status_code
        message = f"openai http {status}: {type(exc).__name__}"
        rid = request_id or str(getattr(exc, "request_id", None) or "")
        if status >= 500 or status in _RETRYABLE_STATUS:
            raise ProviderRetryable(
                message,
                retry_after=parse_retry_after(exc.response.headers),
                request_id=rid,
                details={"status": status},
            ) from exc
        raise ProviderFatal(message, request_id=rid, details={"status": status}) from exc
    if isinstance(exc, openai.APIConnectionError):  # includes APITimeoutError
        raise ProviderRetryable(f"openai connection error: {type(exc).__name__}") from exc
    raise exc


class OpenAIResponsesProvider:
    def __init__(
        self,
        api_key: str,
        *,
        name: str = "openai",
        client: Any = None,
        timeout: float = 60.0,
        max_retries: int = 2,
    ) -> None:
        self.name = name
        if client is None:
            import openai

            client = openai.AsyncOpenAI(api_key=api_key, timeout=timeout, max_retries=max_retries)
        self._client: Any = client

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
        reasoning: dict[str, Any] = {}
        if "effort" in params:
            reasoning["reasoning"] = {"effort": params["effort"]}
        try:
            resp = await self._client.responses.create(
                model=model,
                instructions=system,
                input=[{"role": m["role"], "content": m["content"]} for m in messages],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": schema_name,
                        "schema": dict(json_schema),
                        "strict": True,
                    }
                },
                max_output_tokens=max_output_tokens,
                store=False,
                **reasoning,
            )
        except ProviderError:
            raise
        except Exception as exc:
            raise_openai_error(exc)
        return self._parse(resp)

    @staticmethod
    def _parse(resp: Any) -> ProviderResponse:
        usage = TokenUsage()
        raw_usage = getattr(resp, "usage", None)
        if raw_usage is not None:
            details = getattr(raw_usage, "input_tokens_details", None)
            usage = TokenUsage(
                input_tokens=raw_usage.input_tokens or 0,
                cached_input_tokens=(getattr(details, "cached_tokens", 0) or 0) if details else 0,
                output_tokens=raw_usage.output_tokens or 0,
            )
        request_id = str(getattr(resp, "_request_id", None) or resp.id or "")
        status = str(resp.status)

        for item in resp.output or []:
            if getattr(item, "type", None) != "message":
                continue
            for part in getattr(item, "content", None) or []:
                if getattr(part, "type", None) == "refusal":
                    text = str(getattr(part, "refusal", "") or "")[:200]
                    raise ProviderRefusal(
                        "openai refusal",
                        usage=usage,
                        request_id=request_id,
                        details={"refusal": text},
                    )
        if status == "incomplete":
            reason = getattr(getattr(resp, "incomplete_details", None), "reason", None)
            raise ProviderInvalidOutput(
                "openai response incomplete",
                usage=usage,
                request_id=request_id,
                details={"reason": reason},
            )
        try:
            data = json.loads(resp.output_text)
        except (TypeError, ValueError) as exc:
            raise ProviderInvalidOutput(
                "openai output is not valid JSON", usage=usage, request_id=request_id
            ) from exc
        if not isinstance(data, dict):
            raise ProviderInvalidOutput(
                "openai output is not a JSON object", usage=usage, request_id=request_id
            )
        return ProviderResponse(
            output_json=data, usage=usage, request_id=request_id, stop_reason=status
        )
