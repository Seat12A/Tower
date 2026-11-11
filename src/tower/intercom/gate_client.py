"""HTTP client of gate for the bot: service token + ``X-Acting-Discord-Id`` of the member
who acts. Permissions are always checked by gate. httpx is imported lazily."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    import httpx

_UNREACHABLE = "TWR-BOT-001"

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


def _error_from(status: int, response: httpx.Response) -> GateError:
    """Build the exception for a non-2xx response (gate error body or unavailable)."""
    try:
        err = response.json()["error"]
        code, message = str(err["code"]), str(err.get("message", ""))
        details = err.get("details")
    except (ValueError, KeyError, TypeError):
        if status >= 500:
            return GateUnavailable(status, _UNREACHABLE, f"Gate answered HTTP {status}")
        return GateError(status, _UNREACHABLE, f"Unexpected answer from gate (HTTP {status})")
    return GateError(status, code, message, details if isinstance(details, dict) else None)


class GateClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        http: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        import httpx

        self._httpx = httpx
        self._base_url = base_url
        self._timeout = timeout
        self._auth = f"Bearer {token}"
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(base_url=base_url, timeout=timeout)

    def __repr__(self) -> str:
        return f"GateClient(base_url={self._base_url!r})"

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def _headers(self, acting: int | None) -> dict[str, str]:
        headers = {"Authorization": self._auth}
        if acting is not None:
            headers["X-Acting-Discord-Id"] = str(acting)
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        acting: int | None,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = await self._http.request(
                method, path, params=params, json=body, headers=self._headers(acting)
            )
        except self._httpx.HTTPError as exc:
            raise GateUnavailable(
                0, _UNREACHABLE, f"Gate unreachable ({type(exc).__name__})"
            ) from None
        if response.status_code >= 400:
            raise _error_from(response.status_code, response)
        try:
            return response.json()
        except ValueError:
            raise GateUnavailable(
                response.status_code, _UNREACHABLE, "Gate answered a non-JSON body"
            ) from None

    # --- acting as a staff member (X-Acting-Discord-Id) --------------------------
    async def list_watched(
        self,
        acting: int,
        *,
        active: bool | None = True,
        platform: str | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if active is not None:
            params["active"] = active
        if platform is not None:
            params["platform"] = platform
        rows: list[dict[str, Any]] = await self._request(
            "GET", "/v1/watched-accounts", acting, params=params
        )
        return rows

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
        body: dict[str, Any] = {"platform": platform, "category": category, "priority": priority}
        if handle is not None:
            body["handle"] = handle
        if external_id is not None:
            body["external_id"] = external_id
        if note is not None:
            body["note"] = note
        created: dict[str, Any] = await self._request(
            "POST", "/v1/watched-accounts", acting, body=body
        )
        return created

    async def update_watched(
        self, acting: int, public_id: str, *, version: int, **changes: Any
    ) -> dict[str, Any]:
        updated: dict[str, Any] = await self._request(
            "PATCH",
            f"/v1/watched-accounts/{quote(public_id, safe='')}",
            acting,
            body={"version": version, **changes},
        )
        return updated

    async def get_dossier(self, acting: int, public_id: str) -> dict[str, Any]:
        dossier: dict[str, Any] = await self._request(
            "GET", f"/v1/dossiers/{quote(public_id, safe='')}", acting
        )
        return dossier

    async def transition_dossier(
        self, acting: int, public_id: str, *, to: str, version: int, note: str | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"to": to, "expected_version": version}
        if note is not None:
            body["reason"] = note
        dossier: dict[str, Any] = await self._request(
            "POST", f"/v1/dossiers/{quote(public_id, safe='')}/transitions", acting, body=body
        )
        return dossier

    async def publish_dossier(
        self, acting: int, public_id: str, *, url: str, version: int
    ) -> dict[str, Any]:
        dossier: dict[str, Any] = await self._request(
            "POST",
            f"/v1/dossiers/{quote(public_id, safe='')}/publish",
            acting,
            body={"url": url, "expected_version": version},
        )
        return dossier

    async def list_incidents(self, acting: int, *, limit: int = 10) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = await self._request(
            "GET", "/v1/errors", acting, params={"limit": limit}
        )
        return rows

    async def resolve_incident(
        self, acting: int, fingerprint: str, *, note: str | None = None
    ) -> dict[str, Any]:
        resolved: dict[str, Any] = await self._request(
            "POST",
            f"/v1/errors/{quote(fingerprint, safe='')}/resolve",
            acting,
            body={"note": note},
        )
        return resolved

    async def get_usage(self, acting: int) -> dict[str, Any]:
        usage: dict[str, Any] = await self._request("GET", "/v1/llm/usage", acting)
        return usage

    # --- as the service itself --------------------------------------------------------
    async def get_cursor(self, name: str) -> str | None:
        body = await self._request("GET", f"/v1/events/cursors/{quote(name, safe='')}", None)
        cursor = body.get("cursor")
        return str(cursor) if cursor is not None else None

    async def put_cursor(self, name: str, cursor: str) -> None:
        await self._request(
            "PUT", f"/v1/events/cursors/{quote(name, safe='')}", None, body={"cursor": cursor}
        )

    async def stream_events(
        self, *, after: str | None, topics: Sequence[str]
    ) -> AsyncIterator[SseEvent]:
        """``GET /v1/events?after=…&topics=…``; yields until the connection drops (then
        raises ``GateUnavailable``)."""
        params: list[tuple[str, str | int | float | bool | None]] = [("topics", t) for t in topics]
        if after is not None:
            params.append(("after", after))
        timeout = self._httpx.Timeout(self._timeout, read=None)
        try:
            async with self._http.stream(
                "GET",
                "/v1/events",
                params=params,
                headers={**self._headers(None), "Accept": "text/event-stream"},
                timeout=timeout,
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise _error_from(response.status_code, response)
                event_id = topic = None
                data: list[str] = []
                async for line in response.aiter_lines():
                    if line:
                        if line.startswith(":"):
                            continue
                        name, _, value = line.partition(":")
                        value = value.removeprefix(" ")
                        if name == "id":
                            event_id = value
                        elif name == "event":
                            topic = value
                        elif name == "data":
                            data.append(value)
                        continue
                    # blank line: end of frame
                    frame_id, frame_topic, frame_data = event_id, topic, data
                    event_id = topic = None
                    data = []
                    if frame_id is None or not frame_data:
                        continue
                    try:
                        payload = json.loads("\n".join(frame_data))
                    except ValueError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    yield SseEvent(frame_id, frame_topic or str(payload.get("topic", "")), payload)
        except self._httpx.HTTPError as exc:
            raise GateUnavailable(
                0, _UNREACHABLE, f"Event stream broken ({type(exc).__name__})"
            ) from None
        raise GateUnavailable(0, _UNREACHABLE, "Event stream closed by gate")
