"""Wikipedia knowledge source (tertiary, CC BY-SA 4.0): a map towards primary sources.

Flow for one query: search (REST) in the requested language edition, then ``en``, then
``fr`` → latest revision id (Action API) → revert-risk check when the revision is younger
than 48 h (Lift Wing) → article text and external links of that **fixed revision**
(``action=parse&oldid=``). The evidence cites the revision (``?oldid=``), never the live page.

Verified (2026-11-11), sources: mediawiki.org API pages, api.wikimedia.org and live calls:
- Search: ``GET https://{lang}.wikipedia.org/w/rest.php/v1/search/page?q=&limit=`` →
  ``{"pages": [{"id", "key", "title", "excerpt", "description", ...}]}`` (live-confirmed).
- Latest revision: ``GET /w/api.php?action=query&prop=revisions&rvprop=ids|timestamp
  &titles=<key>&redirects=1&format=json&formatversion=2`` → ``query.pages[0].revisions[0]``
  ``{revid, parentid, timestamp}``; a missing page has ``"missing": true`` (live-confirmed
  for revisions; ``rvstartid`` + ``rvlimit=2`` returns a revision and its predecessor).
- Fixed revision: ``GET /w/api.php?action=parse&oldid=<id>&prop=text|externallinks|revid
  &disableeditsection=1&disabletoc=1&formatversion=2`` → ``parse.text`` (HTML string),
  ``parse.externallinks`` (list of URLs) (docs: ``oldid`` confirmed; the formatversion=2
  shapes are the standard ones but could NOT be re-checked live: the shared egress IP was
  rate-limited by Wikimedia during the session. Parsing is tolerant: ``text`` may also be
  a ``{"*": html}`` object).
- Revert risk: ``POST https://api.wikimedia.org/service/lw/inference/v1/models/
  revertrisk-language-agnostic:predict`` body ``{"rev_id": int, "lang": "fr"}`` →
  ``{"output": {"prediction": bool, "probabilities": {"true": float, "false": float}}}``
  (live-confirmed, no auth for this volume).
- Limits (docs/connaissances.md, mediawiki.org Rate limits): identified User-Agent, at
  most 3 concurrent requests (module-level semaphore), ``Retry-After`` honoured when ≤ 5 s.
  An anonymous shared IP gets HTTP 429 with a text body (not JSON): mapped to
  ``ATL_RATE_LIMITED``.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from tower.core.adapters.knowledge.html_text import extract_text
from tower.core.adapters.knowledge.http import SourceHttp, cached
from tower.core.domain import error_codes as ec
from tower.core.errors import capture
from tower.core.ports.knowledge import (
    Evidence,
    SourceCache,
    SourceCapabilities,
    SourceQuery,
    SourceUnavailable,
    Tier,
)

__all__ = ["WikipediaSource", "reference_urls"]

NAME = "wikipedia"
LICENSE = "CC BY-SA 4.0"
SEARCH_TTL_S = 86_400
LATEST_TTL_S = 86_400
FIXED_TTL_S = 30 * 86_400
RECENT_REVISION = timedelta(hours=48)
RISK_THRESHOLD = 0.5
FALLBACK_LANGS = ("en", "fr")
MAX_REFERENCES = 100
_LANG = re.compile(r"[a-z]{2,12}(?:-[a-z0-9]{1,8})?")
_EXCLUDED_HOST_SUFFIXES = (
    "wikipedia.org",
    "wikimedia.org",
    "wikidata.org",
    "wikisource.org",
    "wikibooks.org",
    "wikiquote.org",
    "wikinews.org",
    "wikivoyage.org",
    "wiktionary.org",
    "wikimediafoundation.org",
    "mediawiki.org",
    "archive.org",
    "archive.today",
    "archive.is",
    "archive.ph",
    "archive.fo",
    "archive.li",
    "archive.md",
    "archive.vn",
    "webcitation.org",
    "wikiwix.com",
    "arquivo.pt",
)
_SKIP_CLASSES = (
    "mw-editsection",
    "reflist",
    "references",
    "navbox",
    "noprint",
    "metadata",
    "mw-references-wrap",
    "toc",
    "reference",
)

Clock = Callable[[], datetime]

_semaphore: tuple[asyncio.AbstractEventLoop, int, asyncio.Semaphore] | None = None


def _limiter(limit: int) -> asyncio.Semaphore:
    """Process-wide semaphore (created lazily, bound to the running loop)."""
    global _semaphore
    loop = asyncio.get_running_loop()
    if _semaphore is None or _semaphore[0] is not loop or _semaphore[1] != limit:
        _semaphore = (loop, limit, asyncio.Semaphore(limit))
    return _semaphore[2]


def reference_urls(links: Sequence[object]) -> tuple[str, ...]:
    """External links of an article without Wikimedia projects and archive mirrors."""
    seen: dict[str, None] = {}
    for raw in links:
        if not isinstance(raw, str):
            continue
        url = f"https:{raw}" if raw.startswith("//") else raw
        if not url.startswith(("http://", "https://")):
            continue
        host = (urlsplit(url).hostname or "").lower()
        if not host or any(
            host == suffix or host.endswith("." + suffix) for suffix in _EXCLUDED_HOST_SUFFIXES
        ):
            continue
        seen.setdefault(url, None)
        if len(seen) >= MAX_REFERENCES:
            break
    return tuple(seen)


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class WikipediaSource:
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
        concurrency: int = 3,
        cache_ttl_s: int = 7 * 86_400,
        transport: Any = None,
        clock: Clock | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._http = SourceHttp(
            NAME, timeout=timeout, user_agent=user_agent, transport=transport, sleep=sleep
        )
        self._cache = cache
        self._concurrency = min(max(concurrency, 1), 3)
        self._clock: Clock = clock or (lambda: datetime.now(UTC))
        del cache_ttl_s  # fixed TTLs: search/latest 1 day, fixed revision 30 days

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        async with _limiter(self._concurrency):
            return await self._http.json_dict(method, url, **kwargs)

    # --- lookup ----------------------------------------------------------------------

    async def lookup(self, query: SourceQuery) -> Sequence[Evidence]:
        langs: list[str] = []
        for lang in (query.lang.lower(), *FALLBACK_LANGS):
            if _LANG.fullmatch(lang) and lang not in langs:
                langs.append(lang)
        for lang in langs:
            pages = await self._search(lang, query.text, max(query.limit, 1))
            if not pages:
                continue
            found: list[Evidence] = []
            for page in pages[: max(query.limit, 1)]:
                evidence = await self._article(lang, page)
                if evidence is not None:
                    found.append(evidence)
            if found:
                return found
        return []

    async def _search(self, lang: str, text: str, limit: int) -> list[dict[str, str]]:
        request = {"op": "search", "lang": lang, "q": text, "limit": limit}

        async def fetch() -> dict[str, Any]:
            body = await self._json(
                "GET",
                f"https://{lang}.wikipedia.org/w/rest.php/v1/search/page",
                params={"q": text, "limit": limit},
            )
            pages = body.get("pages")
            if not isinstance(pages, list):
                raise self._http.fail("wikipedia: search response", ec.ATL_BAD_RESPONSE)
            return {
                "pages": [
                    {"key": str(p["key"]), "title": str(p.get("title") or p["key"])}
                    for p in pages
                    if isinstance(p, dict) and p.get("key")
                ]
            }

        result = await cached(self._cache, NAME, request, SEARCH_TTL_S, fetch)
        return [p for p in result.get("pages", []) if isinstance(p, dict)]

    async def _latest(self, lang: str, key: str) -> dict[str, Any] | None:
        request = {"op": "latest", "lang": lang, "key": key}

        async def fetch() -> dict[str, Any]:
            body = await self._json(
                "GET",
                f"https://{lang}.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "prop": "revisions",
                    "rvprop": "ids|timestamp",
                    "titles": key,
                    "redirects": 1,
                    "format": "json",
                    "formatversion": 2,
                },
            )
            pages = (body.get("query") or {}).get("pages")
            if not isinstance(pages, list) or not pages or not isinstance(pages[0], dict):
                raise self._http.fail("wikipedia: revisions response", ec.ATL_BAD_RESPONSE)
            page = pages[0]
            if page.get("missing"):
                return {"missing": True}
            revisions = page.get("revisions")
            if not isinstance(revisions, list) or not revisions:
                raise self._http.fail("wikipedia: no revision", ec.ATL_BAD_RESPONSE)
            rev = revisions[0]
            try:
                return {
                    "revid": int(rev["revid"]),
                    "parentid": int(rev.get("parentid") or 0),
                    "timestamp": str(rev.get("timestamp") or ""),
                    "title": str(page.get("title") or key),
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise self._http.fail("wikipedia: revision fields", ec.ATL_BAD_RESPONSE) from exc

        result = await cached(self._cache, NAME, request, LATEST_TTL_S, fetch)
        return None if result.get("missing") else result

    async def _revert_risk(self, lang: str, revid: int) -> float:
        request = {"op": "revert_risk", "lang": lang, "rev_id": revid}

        async def fetch() -> dict[str, Any]:
            body = await self._json(
                "POST",
                "https://api.wikimedia.org/service/lw/inference/v1/models/"
                "revertrisk-language-agnostic:predict",
                json_body={"rev_id": revid, "lang": lang},
            )
            try:
                value = float(body["output"]["probabilities"]["true"])
            except (KeyError, TypeError, ValueError) as exc:
                raise self._http.fail(
                    "wikipedia: revert risk response", ec.ATL_BAD_RESPONSE
                ) from exc
            return {"probability": value}

        result = await cached(self._cache, NAME, request, FIXED_TTL_S, fetch)
        return float(result["probability"])

    async def _content(self, lang: str, revid: int) -> dict[str, Any]:
        request = {"op": "content", "lang": lang, "revid": revid}

        async def fetch() -> dict[str, Any]:
            body = await self._json(
                "GET",
                f"https://{lang}.wikipedia.org/w/api.php",
                params={
                    "action": "parse",
                    "oldid": revid,
                    "prop": "text|externallinks|revid",
                    "disableeditsection": 1,
                    "disabletoc": 1,
                    "format": "json",
                    "formatversion": 2,
                },
            )
            parse = body.get("parse")
            if not isinstance(parse, dict):
                raise self._http.fail("wikipedia: parse response", ec.ATL_BAD_RESPONSE)
            html = parse.get("text")
            if isinstance(html, dict):
                html = html.get("*")
            if not isinstance(html, str):
                raise self._http.fail("wikipedia: parse text", ec.ATL_BAD_RESPONSE)
            links = parse.get("externallinks")
            extracted = extract_text(html, skip_classes=_SKIP_CLASSES)
            return {
                "title": str(parse.get("title") or ""),
                "text": extracted.text,
                "references": list(reference_urls(links if isinstance(links, list) else [])),
            }

        return await cached(self._cache, NAME, request, FIXED_TTL_S, fetch)

    async def _article(self, lang: str, page: Mapping[str, str]) -> Evidence | None:
        latest = await self._latest(lang, page["key"])
        if latest is None:
            return None
        revid = int(latest["revid"])
        timestamp = _parse_ts(latest.get("timestamp"))
        extra: dict[str, str] = {}
        published = timestamp
        if timestamp is not None and self._clock() - timestamp < RECENT_REVISION:
            try:
                probability = await self._revert_risk(lang, revid)
            except SourceUnavailable:
                extra["revert_risk"] = "unknown"
            else:
                extra["revert_probability"] = f"{probability:.3f}"
                if probability >= RISK_THRESHOLD and int(latest.get("parentid") or 0) > 0:
                    capture(
                        ec.ATL_REVISION_RISKY,
                        None,
                        source=NAME,
                        lang=lang,
                        revision=revid,
                        probability=round(probability, 3),
                    )
                    revid = int(latest["parentid"])
                    published = None
                    extra["revert_risk"] = "high_previous_revision_used"
                else:
                    extra["revert_risk"] = "low" if probability < RISK_THRESHOLD else "high"
        content = await self._content(lang, revid)
        text = str(content.get("text") or "")
        if not text:
            return None
        title = str(content.get("title") or latest.get("title") or page.get("title") or "")
        return Evidence(
            source=NAME,
            stable_id=str(revid),
            url=f"https://{lang}.wikipedia.org/w/index.php?oldid={revid}",
            text=text,
            lang=lang,
            license=LICENSE,
            sha256=hashlib.sha256(text.encode()).hexdigest(),
            published_at=published,
            version=str(revid),
            extra={"page": page["key"], **extra},
            title=title or None,
            publisher="Wikipedia",
            tier=Tier.TERTIARY,
            references=tuple(str(u) for u in content.get("references", [])),
        )
