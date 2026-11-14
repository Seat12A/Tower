"""X public embed (oEmbed) adapter: implements ``PublicEmbedClient``.

Endpoint documented at https://docs.x.com/x-for-websites/oembed-api (checked on
2026-11-14): ``GET https://publish.x.com/oembed?url=<post URL>``, no authentication;
the response carries ``author_url`` and an ``html`` blockquote whose first ``<p>`` is
the post text. ``omit_script=1`` (no widget script) and ``dnt=true`` (no
personalisation). A missing, deleted or protected post answers 404;
a 403 is treated as a refusal (retryable), never as a missing post.

Volume is bounded by the caller (docs/ingestion.md, "Vérification des contenus").
httpx is imported lazily; the client honours ``HTTPS_PROXY``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tower.core.domain.verification import EmbedPost, embed_text, handle_from_author_url
from tower.core.ports.platforms import (
    PlatformBadResponse,
    PlatformRateLimited,
    PlatformUnavailable,
)

if TYPE_CHECKING:
    import httpx

__all__ = ["XOEmbedClient"]

DEFAULT_OEMBED_URL = "https://publish.x.com/oembed"
USER_AGENT = "Seat12A-Tower/0.1"
DEFAULT_RETRY_AFTER_S = 900.0


class XOEmbedClient:
    """One instance per process (shared httpx client)."""

    def __init__(
        self,
        http: httpx.AsyncClient | None = None,
        *,
        url: str = DEFAULT_OEMBED_URL,
        timeout: float = 10.0,
    ) -> None:
        import httpx

        self._httpx = httpx
        self._url = url
        self._owns_http = http is None
        self._http: httpx.AsyncClient = (
            http
            if http is not None
            else httpx.AsyncClient(
                timeout=timeout,
                headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
                trust_env=True,
            )
        )

    def __repr__(self) -> str:
        return "XOEmbedClient()"

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def lookup(self, external_id: str) -> EmbedPost | None:
        httpx = self._httpx
        params = {
            "url": f"https://x.com/i/status/{external_id}",
            "omit_script": "1",
            "dnt": "true",
        }
        try:
            resp = await self._http.get(self._url, params=params)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise PlatformUnavailable("X oEmbed transport error") from exc
        status = resp.status_code
        if status == 404:
            return None
        if status == 403:
            # Not "missing": X refusing us must not mark every post unavailable.
            raise PlatformUnavailable("X oEmbed 403", status=status)
        if status == 429:
            raise PlatformRateLimited(
                "X oEmbed 429", retry_after=_retry_after(resp.headers.get("retry-after"))
            )
        if status >= 500:
            raise PlatformUnavailable(f"X oEmbed {status}", status=status)
        if status >= 400:
            raise PlatformBadResponse(f"X oEmbed {status}", status=status)
        try:
            body: Any = resp.json()
        except ValueError as exc:
            raise PlatformBadResponse("X oEmbed invalid JSON", status=status) from exc
        if not isinstance(body, dict) or not isinstance(body.get("html"), str):
            raise PlatformBadResponse("X oEmbed unexpected payload", status=status)
        text = embed_text(body["html"])
        if text is None:
            raise PlatformBadResponse("X oEmbed without post text", status=status)
        author_url = body.get("author_url")
        return EmbedPost(
            text=text,
            author_handle=handle_from_author_url(
                author_url if isinstance(author_url, str) else None
            ),
        )


def _retry_after(value: str | None) -> float:
    try:
        seconds = float(value) if value else DEFAULT_RETRY_AFTER_S
    except ValueError:
        seconds = DEFAULT_RETRY_AFTER_S
    return min(max(seconds, 60.0), 3600.0)
