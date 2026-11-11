"""Anthropic Messages API adapter. The ``anthropic`` SDK is imported lazily (RAM)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, NoReturn

from tower.core.adapters.openai_llm import parse_retry_after
from tower.core.domain.llm import TokenUsage
from tower.core.ports.llm import (
    ProviderError,
    ProviderFatal,
    ProviderInvalidOutput,
    ProviderRefusal,
    ProviderResponse,
    ProviderRetryable,
)

__all__ = ["AnthropicMessagesProvider"]

_RETRYABLE_STATUS = frozenset({408, 409, 429})


def _raise_anthropic_error(exc: Exception) -> NoReturn:
    import anthropic

    if isinstance(exc, anthropic.APIStatusError):
        status = exc.status_code
        message = f"anthropic http {status}: {type(exc).__name__}"
        rid = str(getattr(exc, "request_id", None) or "")
        if status >= 500 or status in _RETRYABLE_STATUS:  # 529 = overloaded
            raise ProviderRetryable(
                message,
                retry_after=parse_retry_after(exc.response.headers),
                request_id=rid,
                details={"status": status},
            ) from exc
        raise ProviderFatal(message, request_id=rid, details={"status": status}) from exc
    if isinstance(exc, anthropic.APIConnectionError):  # includes APITimeoutError
        raise ProviderRetryable(f"anthropic connection error: {type(exc).__name__}") from exc
    raise exc


class AnthropicMessagesProvider:
    def __init__(
        self,
        api_key: str,
        *,
        name: str = "anthropic",
        client: Any = None,
        timeout: float = 60.0,
        max_retries: int = 2,
    ) -> None:
        self.name = name
        if client is None:
            import anthropic

            client = anthropic.AsyncAnthropic(
                api_key=api_key, timeout=timeout, max_retries=max_retries
            )
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
        # Never send temperature/top_p/top_k/thinking/tools/tool_choice: effort is the only knob.
        oc: dict[str, Any] = {"format": {"type": "json_schema", "schema": dict(json_schema)}}
        if "effort" in params:
            oc["effort"] = params["effort"]
        try:
            msg = await self._client.messages.create(
                model=model,
                max_tokens=max_output_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": m["role"], "content": m["content"]} for m in messages],
                output_config=oc,
            )
        except ProviderError:
            raise
        except Exception as exc:
            _raise_anthropic_error(exc)
        return self._parse(msg)

    @staticmethod
    def _parse(msg: Any) -> ProviderResponse:
        raw = msg.usage
        cache_read = getattr(raw, "cache_read_input_tokens", None) or 0
        cache_write = getattr(raw, "cache_creation_input_tokens", None) or 0
        usage = TokenUsage(
            input_tokens=(raw.input_tokens or 0) + cache_read + cache_write,
            cached_input_tokens=cache_read,
            output_tokens=raw.output_tokens or 0,
        )
        request_id = str(getattr(msg, "_request_id", None) or msg.id or "")
        stop = msg.stop_reason
        stop_reason = str(stop) if stop is not None else ""

        if stop == "refusal":
            category = getattr(getattr(msg, "stop_details", None), "category", None)
            details = {"category": category} if category else {}
            raise ProviderRefusal(
                "anthropic refusal", usage=usage, request_id=request_id, details=details
            )
        if stop == "max_tokens":
            raise ProviderInvalidOutput(
                "anthropic output truncated (max_tokens)", usage=usage, request_id=request_id
            )
        text: str | None = None
        for block in msg.content or []:
            if getattr(block, "type", None) == "text":
                text = block.text
                break
        try:
            if text is None:
                raise ValueError("no text block")
            data = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise ProviderInvalidOutput(
                "anthropic output is not valid JSON", usage=usage, request_id=request_id
            ) from exc
        if not isinstance(data, dict):
            raise ProviderInvalidOutput(
                "anthropic output is not a JSON object", usage=usage, request_id=request_id
            )
        return ProviderResponse(
            output_json=data, usage=usage, request_id=request_id, stop_reason=stop_reason
        )
