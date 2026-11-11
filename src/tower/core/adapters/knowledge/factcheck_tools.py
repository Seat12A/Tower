"""Google Fact Check Tools (``claims:search``): existing fact-checks in ClaimReview format.

Secondary tier. The evidence stores the review link, title, rating and date, never a copy
of the fact-check.

Verified (2026-11-11), developers.google.com/fact-check/tools/api/reference/rest:
- ``GET https://factchecktools.googleapis.com/v1alpha1/claims:search`` with ``key=<API key>``
  (query parameter), ``query`` (required unless ``reviewPublisherSiteFilter``),
  ``languageCode`` (BCP-47, region ignored), ``pageSize`` (default 10), ``pageToken``,
  ``offset``, ``maxAgeDays``, ``reviewPublisherSiteFilter``.
- Response: ``claims[]`` {``text``, ``claimant``, ``claimDate``, ``claimReview[]``
  {``publisher`` {``name``, ``site``}, ``url``, ``title``, ``reviewDate``, ``textualRating``,
  ``languageCode``}}, ``nextPageToken``. An empty result is ``{}``.
- NOT confirmed: the free quota (visible in the Google console only). An unauthenticated
  request gets HTTP 403, mapped to ``ATL_AUTH_FAILED``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from tower.core.adapters.knowledge.http import SourceHttp, cached
from tower.core.domain import error_codes as ec
from tower.core.ports.knowledge import (
    Evidence,
    SourceCache,
    SourceCapabilities,
    SourceQuery,
    SourceUnavailable,
    Tier,
)

__all__ = ["FactCheckToolsSource"]

NAME = "factcheck_tools"
API = "https://factchecktools.googleapis.com/v1alpha1/claims:search"
TTL_S = 86_400
LICENSE = "Google Fact Check Tools (ClaimReview): link and rating only"


def _date(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class FactCheckToolsSource:
    name = NAME
    capabilities = SourceCapabilities()
    reliability_tier = Tier.SECONDARY
    cost_per_call_usd_micros = 0
    license = LICENSE

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
        return "FactCheckToolsSource()"

    async def aclose(self) -> None:
        await self._http.aclose()

    async def lookup(self, query: SourceQuery) -> Sequence[Evidence]:
        if not self._key:
            raise SourceUnavailable(
                "factcheck_tools: no API key", code=ec.ATL_NOT_CONFIGURED, source=NAME
            )
        lang = query.lang.lower().split("-")[0]
        limit = max(1, min(query.limit, 20))
        request = {"query": query.text, "languageCode": lang, "pageSize": limit}

        async def fetch() -> dict[str, Any]:
            body = await self._http.json_dict("GET", API, params={**request, "key": self._key})
            claims = body.get("claims", [])
            if not isinstance(claims, list):
                raise self._http.fail("factcheck_tools: claims", ec.ATL_BAD_RESPONSE)
            return {"claims": claims}

        result = await cached(self._cache, NAME, request, TTL_S, fetch)
        out: list[Evidence] = []
        seen: set[str] = set()
        for claim in result.get("claims", []):
            if not isinstance(claim, dict):
                continue
            for review in claim.get("claimReview") or []:
                if not isinstance(review, dict) or not review.get("url"):
                    continue
                url = str(review["url"])
                if url in seen:
                    continue
                seen.add(url)
                out.append(self._evidence(claim, review, lang))
        return out

    @staticmethod
    def _evidence(claim: dict[str, Any], review: dict[str, Any], lang: str) -> Evidence:
        raw_publisher = review.get("publisher")
        publisher_obj: dict[str, Any] = raw_publisher if isinstance(raw_publisher, dict) else {}
        publisher = str(publisher_obj.get("name") or publisher_obj.get("site") or "") or None
        rating = str(review.get("textualRating") or "n/a")
        reviewed = _date(review.get("reviewDate"))
        date = reviewed.date().isoformat() if reviewed else "date unknown"
        text = f"{claim.get('text', '')} — {publisher or 'unknown publisher'}: {rating} ({date})"
        extra: dict[str, str] = {}
        if claim.get("claimant"):
            extra["claimant"] = str(claim["claimant"])
        if claim.get("claimDate"):
            extra["claim_date"] = str(claim["claimDate"])
        return Evidence(
            source=NAME,
            stable_id=str(review["url"]),
            url=str(review["url"]),
            text=text,
            lang=str(review.get("languageCode") or lang),
            license=LICENSE,
            sha256=hashlib.sha256(text.encode()).hexdigest(),
            published_at=reviewed,
            extra=extra,
            title=str(review["title"]) if review.get("title") else None,
            publisher=publisher,
        )
