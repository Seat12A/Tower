"""GDELT DOC 2.0 knowledge source (open data, attribution): recent article headlines.

Returns title + domain + date only; articles are never fetched.

Verified (2026-11-14), https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/ and one
live call:
- Endpoint ``https://api.gdeltproject.org/api/v2/doc/doc`` with ``query``, ``mode``
  (``ArtList`` among many timeline/image modes), ``format`` (``JSON``; ``HTML`` default),
  ``maxrecords`` (default 75, maximum 250), ``timespan`` (number + ``min``/``h``/``d``/
  ``w``/``m``, minimum 15 minutes, default 3 months), ``sort`` (``DateDesc``, ``HybridRel``…),
  ``startdatetime``/``enddatetime`` (``YYYYMMDDHHMMSS``, both within the last 3 months).
- Query operators inside ``query``: ``"exact phrase"``, ``(a OR b)`` (capital OR, no nesting),
  ``-`` exclusion, ``domain:``/``domainis:``, ``sourcecountry:`` (2-letter FIPS code, or full
  name without spaces), ``sourcelang:`` (name or 3-letter code), ``theme:``.
- Live-confirmed ``articles[]`` fields: ``url``, ``url_mobile``, ``title``, ``seendate``
  (``YYYYMMDDTHHMMSSZ``), ``socialimage``, ``domain``, ``language`` (name, e.g. "French"),
  ``sourcecountry`` (name, e.g. "France"). Titles carry stray spaces around punctuation.
- NOT stated in the doc: the rate limit. Observed: about 1 request / 5 s, HTTP 429 beyond
  (hence the process-wide minimum interval below); a non-JSON body means a query error.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from tower.core.adapters.knowledge.http import SourceHttp, cached
from tower.core.domain import error_codes as ec
from tower.core.ports.knowledge import (
    Evidence,
    SourceCache,
    SourceCapabilities,
    SourceQuery,
    Tier,
)

__all__ = ["GdeltSource", "MinInterval"]

NAME = "gdelt"
LICENSE = "GDELT (open, attribution)"
API = "https://api.gdeltproject.org/api/v2/doc/doc"
RECENT_TTL_S = 3_600
HISTORICAL_TTL_S = 86_400
MIN_INTERVAL_S = 5.0
MAX_RECORDS = 25
MAX_TERMS = 5
MAX_AGE = timedelta(days=90)
_WORD = re.compile(r"\w+", re.UNICODE)
_STOP = frozenset(
    [
        "le",
        "la",
        "les",
        "un",
        "une",
        "des",
        "du",
        "de",
        "et",
        "ou",
        "en",
        "au",
        "aux",
        "est",
        "sont",
        "que",
        "qui",
        "dans",
        "pour",
        "sur",
        "par",
        "avec",
        "sans",
        "the",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "at",
        "is",
        "are",
        "for",
        "with",
        "by",
        "from",
        "not",
    ]
)
_LANG_NAMES = {"fr": "french", "en": "english", "de": "german", "es": "spanish", "it": "italian"}
_LANG_CODES = {v: k for k, v in _LANG_NAMES.items()}
_FIPS = {
    "FR": "FR", "BE": "BE", "CH": "SZ", "CA": "CA", "GB": "UK", "DE": "GM", "US": "US",
    "ES": "SP", "IT": "IT",
}  # fmt: skip
_KEEP = ("url", "title", "seendate", "domain", "language", "sourcecountry")
Sleep = Callable[[float], Awaitable[None]]


class MinInterval:
    """Process-wide minimum delay between two calls (asyncio lock + monotonic clock)."""

    def __init__(
        self,
        interval_s: float = MIN_INTERVAL_S,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleep | None = None,
    ) -> None:
        self._interval = interval_s
        self._clock = clock
        self._sleep: Sleep = sleep or asyncio.sleep
        self._lock = asyncio.Lock()
        self._last: float | None = None

    async def wait(self) -> None:
        async with self._lock:
            if self._last is not None:
                delay = self._last + self._interval - self._clock()
                if delay > 0:
                    await self._sleep(delay)
            self._last = self._clock()


_SHARED = MinInterval()


def _query(query: SourceQuery) -> str:
    seen: dict[str, None] = {}
    for word in _WORD.findall(query.text.lower()):
        if len(word) >= 3 and word not in _STOP and not word.isdigit():
            seen.setdefault(word, None)
    parts = sorted(seen, key=lambda w: -len(w))[:MAX_TERMS]
    if query.lang in _LANG_NAMES:
        parts.append(f"sourcelang:{_LANG_NAMES[query.lang]}")
    if query.jurisdiction and query.jurisdiction in _FIPS:
        parts.append(f"sourcecountry:{_FIPS[query.jurisdiction]}")
    return " ".join(parts)


def _seen(raw: str) -> datetime | None:
    try:
        return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


class GdeltSource:
    name = NAME
    capabilities = SourceCapabilities()
    reliability_tier = Tier.TERTIARY
    cost_per_call_usd_micros = 0
    license = LICENSE

    def __init__(
        self,
        *,
        user_agent: str,
        timeout: float,
        cache: SourceCache | None = None,
        transport: Any = None,
        sleep: Sleep | None = None,
        interval: MinInterval | None = None,
    ) -> None:
        self._http = SourceHttp(
            NAME, timeout=timeout, user_agent=user_agent, transport=transport, sleep=sleep
        )
        self._cache = cache
        self._interval = interval or _SHARED

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _articles(self, params: dict[str, str], ttl_s: int) -> list[dict[str, Any]]:
        async def fetch() -> dict[str, Any]:
            await self._interval.wait()
            response = await self._http.request("GET", API, params=params)
            body = response.text.strip()
            if not body:
                return {"articles": []}
            try:
                data = json.loads(body)
            except ValueError as exc:
                raise self._http.fail("gdelt: response is not JSON", ec.ATL_BAD_RESPONSE) from exc
            if not isinstance(data, dict):
                raise self._http.fail("gdelt: JSON object expected", ec.ATL_BAD_RESPONSE)
            articles = data.get("articles", [])
            if not isinstance(articles, list):
                raise self._http.fail("gdelt: articles list expected", ec.ATL_BAD_RESPONSE)
            return {
                "articles": [{k: a.get(k) for k in _KEEP} for a in articles if isinstance(a, dict)]
            }

        data = await cached(self._cache, NAME, params, ttl_s, fetch)
        return list(data.get("articles", []))

    async def lookup(self, query: SourceQuery) -> Sequence[Evidence]:
        q = _query(query)
        if not any(c.isalnum() for c in q.split(" sourcelang:")[0].split(" sourcecountry:")[0]):
            return []
        params = {"query": q, "mode": "artlist", "format": "json", "sort": "datedesc",
                  "maxrecords": str(MAX_RECORDS)}  # fmt: skip
        ttl = RECENT_TTL_S
        if query.as_of is not None:
            end = query.as_of if query.as_of.tzinfo else query.as_of.replace(tzinfo=UTC)
            if end < datetime.now(UTC) - MAX_AGE:
                return []  # the DOC API only covers the last 3 months
            params["startdatetime"] = (end - timedelta(days=30)).strftime("%Y%m%d%H%M%S")
            params["enddatetime"] = end.strftime("%Y%m%d%H%M%S")
            ttl = HISTORICAL_TTL_S
        else:
            params["timespan"] = "7d"
        out: list[Evidence] = []
        urls: set[str] = set()
        for item in await self._articles(params, ttl):
            url = str(item.get("url") or "")
            title = " ".join(str(item.get("title") or "").split())
            domain = str(item.get("domain") or "")
            if not url.startswith(("http://", "https://")) or not title or url in urls:
                continue
            urls.add(url)
            when = _seen(str(item.get("seendate") or ""))
            body = f"{domain}, {when.date().isoformat() if when else 'n.d.'}: {title}"
            language = _LANG_CODES.get(str(item.get("language") or "").lower(), "und")
            out.append(
                Evidence(
                    source=NAME,
                    stable_id=hashlib.sha256(url.encode()).hexdigest()[:16],
                    url=url,
                    text=body,
                    lang=language,
                    license=LICENSE,
                    sha256=hashlib.sha256(body.encode()).hexdigest(),
                    published_at=when,
                    extra={"origin": "gdelt", "country": str(item.get("sourcecountry") or "")},
                    title=title,
                    publisher=domain or None,
                    tier=Tier.TERTIARY,
                )
            )
            if len(out) >= max(query.limit, 1):
                break
        return out
