"""HTTP client of gate for the bot: service token + ``X-Acting-Discord-Id`` of the member
who acts. Permissions are always checked by gate. httpx is imported lazily."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx

__all__ = ["GateClient", "GateError", "GateUnavailable", "SseEvent"]


class GateError(Exception):
    """Error answered by gate: ``code`` is the ``TWR-*`` code of the response body."""

    def __init__(
        self, status: int, code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


class GateUnavailable(GateError):
    """Network error, timeout or 5xx without a body (``TWR-BOT-001``)."""


@dataclass(frozen=True, slots=True)
class SseEvent:
    id: str
    """Cursor ``<xid>-<id>``."""
    topic: str
    data: dict[str, Any] = field(default_factory=dict)
    """Public event: ``{"id", "topic", "aggregate_type", "aggregate_id", "payload", ...}``."""


class GateClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        http: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        raise NotImplementedError

    async def aclose(self) -> None:
        raise NotImplementedError

    # --- acting as a staff member (X-Acting-Discord-Id) --------------------------
    async def list_watched(
        self,
        acting: int,
        *,
        active: bool | None = True,
        platform: str | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def add_watched(
        self,
        acting: int,
        *,
        platform: str,
        category: str,
        handle: str | None = None,
        external_id: str | None = None,
        priority: int = 0,
        note: str | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def update_watched(
        self, acting: int, public_id: str, *, version: int, **changes: Any
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def get_dossier(self, acting: int, public_id: str) -> dict[str, Any]:
        raise NotImplementedError

    async def transition_dossier(
        self, acting: int, public_id: str, *, to: str, version: int, note: str | None = None
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def publish_dossier(
        self, acting: int, public_id: str, *, url: str, version: int
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def list_incidents(self, acting: int, *, limit: int = 10) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def resolve_incident(
        self, acting: int, fingerprint: str, *, note: str | None = None
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def get_usage(self, acting: int) -> dict[str, Any]:
        raise NotImplementedError

    # --- as the service itself --------------------------------------------------------
    async def get_cursor(self, name: str) -> str | None:
        raise NotImplementedError

    async def put_cursor(self, name: str, cursor: str) -> None:
        raise NotImplementedError

    def stream_events(self, *, after: str | None, topics: Sequence[str]) -> AsyncIterator[SseEvent]:
        """``GET /v1/events?after=…&topics=…``; yields until the connection drops (then
        raises ``GateUnavailable``)."""
        raise NotImplementedError
