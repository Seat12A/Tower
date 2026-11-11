"""Brave Search API adapter (``SearchClient``).

Verified (2026-11-11), api-dashboard.search.brave.com documentation:
- ``GET https://api.search.brave.com/res/v1/web/search`` with header
  ``X-Subscription-Token: <key>`` (and ``Accept: application/json``).
- Parameters: ``q`` (operators go inside ``q``), ``count`` (max 20, default 20), ``offset``
  (0-based page, max 9), ``country`` (2 letters), ``search_lang``, ``freshness``
  (``pd|pw|pm|py`` or a date range), ``safesearch``, ``extra_snippets``.
- Operators: ``site:`` (a partial domain includes its subdomains), ``"phrase"``, ``-term``,
  ``filetype:``, and uppercase ``OR`` / ``NOT`` (documented example
  ``electric vehicles site:reuters.com OR site:bloomberg.com 2025``).
- Response: ``web.results[]`` with ``title``, ``url``, ``description`` (confirmed);
  ``page_age`` (ISO date) and ``language`` are read when present (NOT confirmed in the
  pages fetched). Rate-limit headers and 429 body: NOT confirmed. Unconfirmed query length
  limit: at most 10 domains go in one ``site:`` OR group, and results are post-filtered by
  domain anyway (a domain restriction never relies on the engine alone).
- Cost: about 5 USD per 1 000 requests (docs/connaissances.md).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from tower.core.adapters.knowledge.http import SourceHttp, cached
from tower.core.domain import error_codes as ec
from tower.core.ports.knowledge import SourceCache, SourceUnavailable
from tower.core.ports.web import SearchResult

__all__ = ["BraveSearch"]

NAME = "brave"
API = "https://api.search.brave.com/res/v1/web/search"
TTL_S = 86_400
MAX_SITE_TERMS = 10
_DOMAIN = re.compile(r"[a-z0-9.-]+")


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _in_domains(url: str, domains: Sequence[str]) -> bool:
    host = _host(url)
    return any(host == d or host.endswith("." + d) for d in domains)


def _parse_age(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class BraveSearch:
    name = NAME

    def __init__(
        self,
        *,
        api_key: str | None,
        user_agent: str,
        timeout: float,
        cache: SourceCache | None = None,
        transport: Any = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._key = api_key
        self._http = SourceHttp(
            NAME, timeout=timeout, user_agent=user_agent, transport=transport, sleep=sleep
        )
        self._cache = cache

    def __repr__(self) -> str:
        return "BraveSearch()"

    async def aclose(self) -> None:
        await self._http.aclose()

    @staticmethod
    def build_query(
        query: str, include_domains: Sequence[str], exclude_domains: Sequence[str]
    ) -> str:
        include = [
            d for d in dict.fromkeys(d.lower() for d in include_domains) if _DOMAIN.fullmatch(d)
        ]
        parts = [query.strip()]
        if include:
            group = " OR ".join(f"site:{d}" for d in include[:MAX_SITE_TERMS])
            parts.append(f"({group})" if len(include[:MAX_SITE_TERMS]) > 1 else group)
        for domain in exclude_domains:
            if _DOMAIN.fullmatch(domain.lower()):
                parts.append(f"-site:{domain.lower()}")
        return " ".join(p for p in parts if p)

    async def search(
        self,
        query: str,
        *,
        limit: int,
        lang: str | None = None,
        country: str | None = None,
        include_domains: Sequence[str] = (),
        exclude_domains: Sequence[str] = (),
    ) -> Sequence[SearchResult]:
        if not self._key:
            raise SourceUnavailable("brave: no API key", code=ec.ATL_NOT_CONFIGURED, source=NAME)
        params: dict[str, Any] = {
            "q": self.build_query(query, include_domains, exclude_domains),
            "count": max(1, min(limit, 20)),
        }
        if country:
            params["country"] = country.upper()[:2]
        if lang:
            params["search_lang"] = lang.lower().split("-")[0]
        key = self._key

        async def fetch() -> dict[str, Any]:
            body = await self._http.json_dict(
                "GET",
                API,
                params=params,
                headers={"X-Subscription-Token": key, "Accept": "application/json"},
            )
            web = body.get("web")
            if web is None:
                return {"results": []}  # no web results section: empty answer
            results = web.get("results") if isinstance(web, dict) else None
            if not isinstance(results, list):
                raise self._http.fail("brave: web.results", ec.ATL_BAD_RESPONSE)
            return {"results": results}

        result = await cached(self._cache, NAME, params, TTL_S, fetch)
        include = [d.lower() for d in include_domains]
        exclude = [d.lower() for d in exclude_domains]
        out: list[SearchResult] = []
        for item in result.get("results", []):
            if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                continue
            url = item["url"]
            if include and not _in_domains(url, include):
                continue
            if exclude and _in_domains(url, exclude):
                continue
            out.append(
                SearchResult(
                    url=url,
                    title=str(item.get("title") or ""),
                    snippet=str(item.get("description") or ""),
                    published_at=_parse_age(item.get("page_age")),
                    lang=str(item["language"]) if item.get("language") else None,
                )
            )
            if len(out) >= limit:
                break
        return out
