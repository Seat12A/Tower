"""Shared HTTP plumbing of the Atlas adapters: one httpx client per adapter, uniform
error mapping to :class:`SourceUnavailable`, and the ``source_cache`` read-through helper.

httpx is imported lazily (in :class:`SourceHttp`'s constructor).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

from tower.core.domain import error_codes as ec
from tower.core.ports.knowledge import SourceCache, SourceUnavailable

if TYPE_CHECKING:
    import httpx

__all__ = ["MAX_RETRY_AFTER_S", "SourceHttp", "cached"]

MAX_RETRY_AFTER_S = 5.0
"""``Retry-After`` honoured (one sleep, one retry) only up to this delay."""

Sleep = Callable[[float], Awaitable[None]]


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return None  # HTTP-date form: treated as "too long"


class SourceHttp:
    """One ``httpx.AsyncClient`` for one adapter. Failures become ``SourceUnavailable``.

    Secrets never appear in messages: only the adapter name and the status are reported.
    """

    def __init__(
        self,
        source: str,
        *,
        timeout: float,
        user_agent: str,
        transport: Any = None,
        headers: Mapping[str, str] | None = None,
        sleep: Sleep | None = None,
    ) -> None:
        import httpx

        self._httpx = httpx
        self.source = source
        self._sleep: Sleep = sleep or asyncio.sleep
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
            headers={"User-Agent": user_agent, **(headers or {})},
            follow_redirects=True,
            trust_env=transport is None,  # honour HTTPS_PROXY; tests inject a transport
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def fail(self, message: str, code: ec.ErrorCode) -> SourceUnavailable:
        return SourceUnavailable(message, code=code, source=self.source)

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Send the request; 429 with ``Retry-After`` ≤ 5 s sleeps once then retries."""
        httpx = self._httpx
        for attempt in (1, 2):
            try:
                response = await self._client.request(
                    method, url, params=params, data=data, json=json_body, headers=headers
                )
            except httpx.TimeoutException as exc:
                raise self.fail(f"{self.source}: timeout", ec.ATL_SOURCE_UNAVAILABLE) from exc
            except httpx.HTTPError as exc:
                raise self.fail(
                    f"{self.source}: network error ({type(exc).__name__})",
                    ec.ATL_SOURCE_UNAVAILABLE,
                ) from exc
            status = response.status_code
            if status == 429:
                delay = _retry_after(response)
                if attempt == 1 and delay is not None and delay <= MAX_RETRY_AFTER_S:
                    await self._sleep(delay)
                    continue
                raise self.fail(f"{self.source}: HTTP 429", ec.ATL_RATE_LIMITED)
            if status in (401, 403):
                raise self.fail(f"{self.source}: HTTP {status}", ec.ATL_AUTH_FAILED)
            if status >= 500:
                raise self.fail(f"{self.source}: HTTP {status}", ec.ATL_SOURCE_UNAVAILABLE)
            if status >= 400:
                raise self.fail(f"{self.source}: HTTP {status}", ec.ATL_BAD_RESPONSE)
            return response
        raise self.fail(f"{self.source}: HTTP 429", ec.ATL_RATE_LIMITED)  # pragma: no cover

    async def json(self, method: str, url: str, **kwargs: Any) -> Any:
        response = await self.request(method, url, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise self.fail(f"{self.source}: response is not JSON", ec.ATL_BAD_RESPONSE) from exc

    async def json_dict(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        body = await self.json(method, url, **kwargs)
        if not isinstance(body, dict):
            raise self.fail(f"{self.source}: JSON object expected", ec.ATL_BAD_RESPONSE)
        return body


async def cached(
    cache: SourceCache | None,
    adapter: str,
    request: Mapping[str, object],
    ttl_s: int,
    fetch: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Read-through helper: cache hit → no HTTP call; miss → ``fetch`` then store.

    ``request`` is the canonical request dict (never containing a secret).
    """
    if cache is not None:
        hit = await cache.get(adapter, request)
        if hit is not None:
            return hit
    value = await fetch()
    if cache is not None:
        await cache.put(adapter, request, value, ttl_s=ttl_s)
    return value
