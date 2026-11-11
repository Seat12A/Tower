"""Provider adapters and strict schema helper: fake clients, no network, no database."""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace as NS
from typing import Any

import httpx2
import pytest
from pydantic import BaseModel, SecretStr

from tower.core.adapters.anthropic_llm import AnthropicMessagesProvider
from tower.core.adapters.factory import build_embedder, build_llm_providers
from tower.core.adapters.openai_embedder import OpenAIEmbedder
from tower.core.adapters.openai_llm import OpenAIResponsesProvider
from tower.core.config import Settings
from tower.core.domain.llm import TokenUsage
from tower.core.llm.schema import schema_name, strict_json_schema
from tower.core.ports.llm import (
    ProviderFatal,
    ProviderInvalidOutput,
    ProviderRefusal,
    ProviderRetryable,
)

SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}}
CALL: dict[str, Any] = {
    "model": "m",
    "system": "sys",
    "messages": [{"role": "user", "content": "hi"}],
    "json_schema": SCHEMA,
    "schema_name": "Out",
    "max_output_tokens": 100,
}
FORBIDDEN = {"temperature", "top_p", "top_k", "tool_choice", "tools", "thinking"}


class Recorder:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result = result
        self.error = error

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


def openai_resp(**over: Any) -> Any:
    base: dict[str, Any] = {
        "id": "resp_1",
        "status": "completed",
        "output": [NS(type="message", content=[NS(type="output_text", text="{}")])],
        "output_text": '{"a": "x"}',
        "usage": NS(input_tokens=10, input_tokens_details=NS(cached_tokens=4), output_tokens=5),
        "incomplete_details": None,
    }
    base.update(over)
    return NS(**base)


def openai_provider(rec: Recorder) -> OpenAIResponsesProvider:
    return OpenAIResponsesProvider("k", client=NS(responses=rec))


def anthropic_msg(**over: Any) -> Any:
    base: dict[str, Any] = {
        "id": "msg_1",
        "stop_reason": "end_turn",
        "content": [NS(type="thinking", thinking="..."), NS(type="text", text='{"a": "x"}')],
        "usage": NS(
            input_tokens=10,
            cache_read_input_tokens=100,
            cache_creation_input_tokens=20,
            output_tokens=7,
        ),
    }
    base.update(over)
    return NS(**base)


def anthropic_provider(rec: Recorder) -> AnthropicMessagesProvider:
    return AnthropicMessagesProvider("k", client=NS(messages=rec))


def http_response(status: int, headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(
        status,
        headers=headers or {},
        request=httpx2.Request("POST", "https://example.invalid/v1"),
    )


# --------------------------------------------------------------------- OpenAI


async def test_openai_kwargs_exact() -> None:
    rec = Recorder(openai_resp())
    out = await openai_provider(rec).complete(**CALL, params={"effort": "low", "other": 1})
    assert rec.calls == [
        {
            "model": "m",
            "instructions": "sys",
            "input": [{"role": "user", "content": "hi"}],
            "text": {
                "format": {"type": "json_schema", "name": "Out", "schema": SCHEMA, "strict": True}
            },
            "max_output_tokens": 100,
            "store": False,
            "reasoning": {"effort": "low"},
        }
    ]
    assert not FORBIDDEN & rec.calls[0].keys()
    assert out.output_json == {"a": "x"}
    assert out.usage == TokenUsage(input_tokens=10, cached_input_tokens=4, output_tokens=5)
    assert out.request_id == "resp_1"
    assert out.stop_reason == "completed"


async def test_openai_no_effort_no_reasoning() -> None:
    rec = Recorder(
        openai_resp(
            _request_id="req_9",
            usage=NS(input_tokens=1, input_tokens_details=None, output_tokens=2),
        )
    )
    out = await openai_provider(rec).complete(**CALL, params={})
    assert "reasoning" not in rec.calls[0]
    assert out.request_id == "req_9"
    assert out.usage == TokenUsage(1, 0, 2)


async def test_openai_refusal() -> None:
    resp = openai_resp(
        output=[NS(type="message", content=[NS(type="refusal", refusal="no" * 300)])]
    )
    with pytest.raises(ProviderRefusal) as ei:
        await openai_provider(Recorder(resp)).complete(**CALL, params={})
    assert len(ei.value.details["refusal"]) == 200
    assert ei.value.usage.output_tokens == 5
    assert ei.value.request_id == "resp_1"


async def test_openai_incomplete() -> None:
    resp = openai_resp(status="incomplete", incomplete_details=NS(reason="max_output_tokens"))
    with pytest.raises(ProviderInvalidOutput) as ei:
        await openai_provider(Recorder(resp)).complete(**CALL, params={})
    assert ei.value.details["reason"] == "max_output_tokens"
    assert ei.value.usage.input_tokens == 10


@pytest.mark.parametrize("text", ["not json", "[1, 2]", "null"])
async def test_openai_bad_output(text: str) -> None:
    with pytest.raises(ProviderInvalidOutput) as ei:
        await openai_provider(Recorder(openai_resp(output_text=text))).complete(**CALL, params={})
    assert ei.value.request_id == "resp_1"


# ------------------------------------------------------------------ Anthropic


async def test_anthropic_kwargs_exact_and_usage() -> None:
    rec = Recorder(anthropic_msg())
    out = await anthropic_provider(rec).complete(**CALL, params={"effort": "high"})
    assert rec.calls == [
        {
            "model": "m",
            "max_tokens": 100,
            "system": [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": "hi"}],
            "output_config": {
                "format": {"type": "json_schema", "schema": SCHEMA},
                "effort": "high",
            },
        }
    ]
    assert not FORBIDDEN & rec.calls[0].keys()
    assert out.output_json == {"a": "x"}
    assert out.usage == TokenUsage(input_tokens=130, cached_input_tokens=100, output_tokens=7)
    assert out.request_id == "msg_1"
    assert out.stop_reason == "end_turn"


async def test_anthropic_no_effort_and_none_cache_fields() -> None:
    msg = anthropic_msg(
        _request_id="req_1",
        usage=NS(
            input_tokens=3,
            cache_read_input_tokens=None,
            cache_creation_input_tokens=None,
            output_tokens=1,
        ),
    )
    rec = Recorder(msg)
    out = await anthropic_provider(rec).complete(**CALL, params={})
    assert "effort" not in rec.calls[0]["output_config"]
    assert out.usage == TokenUsage(3, 0, 1)
    assert out.request_id == "req_1"


async def test_anthropic_refusal() -> None:
    msg = anthropic_msg(stop_reason="refusal", stop_details=NS(category="cyber"))
    with pytest.raises(ProviderRefusal) as ei:
        await anthropic_provider(Recorder(msg)).complete(**CALL, params={})
    assert ei.value.details == {"category": "cyber"}
    assert ei.value.usage.input_tokens == 130


async def test_anthropic_refusal_without_details() -> None:
    with pytest.raises(ProviderRefusal) as ei:
        await anthropic_provider(Recorder(anthropic_msg(stop_reason="refusal"))).complete(
            **CALL, params={}
        )
    assert ei.value.details == {}


async def test_anthropic_max_tokens() -> None:
    with pytest.raises(ProviderInvalidOutput):
        await anthropic_provider(Recorder(anthropic_msg(stop_reason="max_tokens"))).complete(
            **CALL, params={}
        )


@pytest.mark.parametrize(
    "content",
    [[NS(type="text", text="oops")], [NS(type="text", text="[]")], [NS(type="thinking")], []],
)
async def test_anthropic_bad_output(content: list[Any]) -> None:
    with pytest.raises(ProviderInvalidOutput):
        await anthropic_provider(Recorder(anthropic_msg(content=content))).complete(
            **CALL, params={}
        )


# ------------------------------------------------------------ exception mapping


def sdk_errors(module: Any) -> list[tuple[Exception, type[Exception], float | None]]:
    req = httpx2.Request("POST", "https://example.invalid/v1")
    return [
        (
            module.RateLimitError(
                "m", response=http_response(429, {"retry-after": "7"}), body=None
            ),
            ProviderRetryable,
            7.0,
        ),
        (
            module.InternalServerError("m", response=http_response(500), body=None),
            ProviderRetryable,
            None,
        ),
        (
            module.APIStatusError(
                "m", response=http_response(529, {"retry-after": "soon"}), body=None
            ),
            ProviderRetryable,
            None,
        ),
        (
            module.APIStatusError("m", response=http_response(409), body=None),
            ProviderRetryable,
            None,
        ),
        (module.APITimeoutError(request=req), ProviderRetryable, None),
        (module.APIConnectionError(message="down", request=req), ProviderRetryable, None),
        (
            module.AuthenticationError("m", response=http_response(401), body=None),
            ProviderFatal,
            None,
        ),
        (
            module.PermissionDeniedError("m", response=http_response(403), body=None),
            ProviderFatal,
            None,
        ),
        (
            module.BadRequestError("m", response=http_response(400), body=None),
            ProviderFatal,
            None,
        ),
        (
            module.NotFoundError("m", response=http_response(404), body=None),
            ProviderFatal,
            None,
        ),
        (
            module.UnprocessableEntityError("m", response=http_response(422), body=None),
            ProviderFatal,
            None,
        ),
        (
            module.APIStatusError("m", response=http_response(418), body=None),
            ProviderFatal,
            None,
        ),
    ]


def _check(exc: Exception, got: BaseException, kind: type[Exception], retry: float | None) -> None:
    assert isinstance(got, kind)
    assert got.__cause__ is exc
    assert "sk-secret" not in str(got)
    if isinstance(got, ProviderRetryable):
        assert got.retry_after == retry


async def test_openai_exception_mapping() -> None:
    import openai

    for exc, kind, retry in sdk_errors(openai):
        provider = OpenAIResponsesProvider("sk-secret", client=NS(responses=Recorder(error=exc)))
        with pytest.raises(ProviderRetryable if kind is ProviderRetryable else ProviderFatal) as ei:
            await provider.complete(**CALL, params={})
        _check(exc, ei.value, kind, retry)


async def test_anthropic_exception_mapping() -> None:
    import anthropic

    for exc, kind, retry in sdk_errors(anthropic):
        provider = AnthropicMessagesProvider("sk-secret", client=NS(messages=Recorder(error=exc)))
        with pytest.raises(ProviderRetryable if kind is ProviderRetryable else ProviderFatal) as ei:
            await provider.complete(**CALL, params={})
        _check(exc, ei.value, kind, retry)


async def test_unknown_exception_propagates() -> None:
    provider = openai_provider(Recorder(error=KeyError("x")))
    with pytest.raises(KeyError):
        await provider.complete(**CALL, params={})


# --------------------------------------------------------------------- embedder


class EmbedRecorder:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        items = [
            NS(index=i, embedding=[float(text), float(i)]) for i, text in enumerate(kwargs["input"])
        ]
        return NS(data=list(reversed(items)))


async def test_embedder_batches_and_orders() -> None:
    rec = EmbedRecorder()
    emb = OpenAIEmbedder("k", client=NS(embeddings=rec), batch_size=2)
    assert emb.model == emb.version == "text-embedding-3-small"
    out = await emb.embed(["1", "2", "3"], dimensions=8)
    assert out == [[1.0, 0.0], [2.0, 1.0], [3.0, 0.0]]
    assert [c["input"] for c in rec.calls] == [["1", "2"], ["3"]]
    assert all(c["dimensions"] == 8 and c["model"] == emb.model for c in rec.calls)


async def test_embedder_empty_makes_no_call() -> None:
    rec = EmbedRecorder()
    assert await OpenAIEmbedder("k", client=NS(embeddings=rec)).embed([], dimensions=8) == []
    assert rec.calls == []


async def test_embedder_error_mapping() -> None:
    import openai

    retry = openai.RateLimitError("m", response=http_response(429, {"retry-after": "2"}), body=None)
    emb = OpenAIEmbedder("k", client=NS(embeddings=EmbedRecorder(retry)))
    with pytest.raises(ProviderRetryable) as ei:
        await emb.embed(["a"], dimensions=4)
    assert ei.value.retry_after == 2.0
    fatal = openai.AuthenticationError("m", response=http_response(401), body=None)
    emb = OpenAIEmbedder("k", client=NS(embeddings=EmbedRecorder(fatal)))
    with pytest.raises(ProviderFatal):
        await emb.embed(["a"], dimensions=4)


# ---------------------------------------------------------------------- factory


def settings(**keys: str | None) -> Settings:
    values = {
        name: SecretStr(key) if (key := keys.get(name)) else None
        for name in ("openai_api_key", "openai_api_key_shared", "anthropic_api_key")
    }
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_factory_all_keys() -> None:
    providers = build_llm_providers(
        settings(openai_api_key="a", openai_api_key_shared="b", anthropic_api_key="c")
    )
    assert set(providers) == {"openai", "openai_shared", "anthropic"}
    assert [p.name for p in providers.values()] == ["openai", "openai_shared", "anthropic"]


def test_factory_partial_and_none() -> None:
    assert set(build_llm_providers(settings(anthropic_api_key="c"))) == {"anthropic"}
    assert set(build_llm_providers(settings(openai_api_key_shared="b"))) == {"openai_shared"}
    assert build_llm_providers(settings()) == {}


def test_factory_embedder() -> None:
    assert build_embedder(settings()) is None
    assert isinstance(build_embedder(settings(openai_api_key_shared="b")), OpenAIEmbedder)
    both = build_embedder(settings(openai_api_key="a", openai_api_key_shared="b"))
    assert isinstance(both, OpenAIEmbedder)
    assert both._client.api_key == "a"  # prefers the non-shared key
    shared_only = build_embedder(settings(openai_api_key_shared="b"))
    assert isinstance(shared_only, OpenAIEmbedder)
    assert shared_only._client.api_key == "b"


# ----------------------------------------------------------------------- schema


class Inner(BaseModel):
    value: int = 3
    note: str | None = None


class Outer(BaseModel):
    title: str
    inner: Inner
    items: list[Inner]
    nickname: str = "x"
    maybe: int | None = None


def _objects(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if "properties" in node:
            found.append(node)
        for v in node.values():
            found.extend(_objects(v))
    elif isinstance(node, list):
        for v in node:
            found.extend(_objects(v))
    return found


def _has_key(node: Any, key: str) -> bool:
    if isinstance(node, dict):
        return key in node or any(_has_key(v, key) for v in node.values())
    if isinstance(node, list):
        return any(_has_key(v, key) for v in node)
    return False


def test_strict_json_schema() -> None:
    original = Outer.model_json_schema()
    schema = strict_json_schema(Outer)
    objects = _objects(schema)
    assert len(objects) == 2  # Outer + Inner in $defs
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert obj["required"] == list(obj["properties"])
    assert not _has_key(schema, "default")
    props = schema["properties"]
    assert props["title"] == {"title": "Title", "type": "string"}
    assert props["nickname"]["anyOf"][-1] == {"type": "null"}
    assert sum(1 for s in props["maybe"]["anyOf"] if s == {"type": "null"}) == 1
    inner = schema["$defs"]["Inner"]["properties"]
    assert inner["value"]["anyOf"][-1] == {"type": "null"}
    assert sum(1 for s in inner["note"]["anyOf"] if s == {"type": "null"}) == 1
    assert props["items"]["items"] == {"$ref": "#/$defs/Inner"}
    assert "default" in original["$defs"]["Inner"]["properties"]["value"]  # original untouched


def test_schema_name() -> None:
    assert schema_name(Outer) == "Outer"
    weird = type("Weird.Name é", (BaseModel,), {})
    assert schema_name(weird) == "Weird_Name__"
    long = type("A" * 100, (BaseModel,), {})
    assert len(schema_name(long)) == 64


# -------------------------------------------------------------- import isolation


def test_adapters_do_not_import_sdks() -> None:
    code = (
        "import tower.core.adapters.openai_llm, tower.core.adapters.anthropic_llm, "
        "tower.core.adapters.openai_embedder, tower.core.adapters.factory, "
        "tower.core.llm.schema, sys; "
        "assert 'openai' not in sys.modules and 'anthropic' not in sys.modules"
    )
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
