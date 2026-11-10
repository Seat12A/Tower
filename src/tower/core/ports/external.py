"""Minimal contracts, to be extended when the corresponding services are written."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class XPost:
    id: int
    author_id: int
    text: str
    created_at: datetime
    metrics: dict[str, int]


class XClient(Protocol):
    """Official X API (paid per read: each call has a cost)."""

    async def get_post(self, post_id: int) -> XPost | None: ...

    async def user_posts(self, user_id: int, *, since_id: int | None) -> Sequence[XPost]: ...


class LLMClient(Protocol):
    """Call a model with structured output; also returns usage for cost tracking."""

    async def complete(
        self, *, model: str, system: str, messages: Sequence[dict[str, Any]], schema: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, int]]: ...


class SearchClient(Protocol):
    async def search(self, query: str, *, limit: int) -> Sequence[dict[str, Any]]: ...


class Notifier(Protocol):
    """Outbound notification outside the event bus (budget alerts, incidents)."""

    async def notify(self, channel: str, message: str, **fields: Any) -> None: ...
