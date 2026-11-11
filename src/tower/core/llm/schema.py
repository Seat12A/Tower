"""JSON Schema helpers for strict structured outputs (OpenAI strict mode, Anthropic)."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

__all__ = ["schema_name", "strict_json_schema"]

_NAME_INVALID = re.compile(r"[^a-zA-Z0-9_-]")


def schema_name(model: type[BaseModel]) -> str:
    """Class name sanitized to ``^[a-zA-Z0-9_-]{1,64}$``."""
    name = _NAME_INVALID.sub("_", model.__name__)[:64]
    return name or "Schema"


def _allows_null(schema: dict[str, Any]) -> bool:
    if schema.get("type") == "null":
        return True
    type_ = schema.get("type")
    if isinstance(type_, list) and "null" in type_:
        return True
    if "const" in schema and schema["const"] is None:
        return True
    enum = schema.get("enum")
    if isinstance(enum, list) and None in enum:
        return True
    any_of = schema.get("anyOf")
    return isinstance(any_of, list) and any(isinstance(s, dict) and _allows_null(s) for s in any_of)


def _walk(node: Any) -> Any:
    if isinstance(node, list):
        return [_walk(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "default":
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {name: _walk(sub) for name, sub in value.items()}
        else:
            out[key] = _walk(value)
    properties = out.get("properties")
    if isinstance(properties, dict):
        original_required = set(node.get("required", []))
        for name, sub in list(properties.items()):
            if name not in original_required and isinstance(sub, dict) and not _allows_null(sub):
                properties[name] = {"anyOf": [sub, {"type": "null"}]}
        out["additionalProperties"] = False
        out["required"] = list(properties)
    return out


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Strict-mode JSON Schema of ``model``: closed objects, all properties required,
    originally optional properties nullable, no ``default`` keywords."""
    result: dict[str, Any] = _walk(model.model_json_schema())
    return result
