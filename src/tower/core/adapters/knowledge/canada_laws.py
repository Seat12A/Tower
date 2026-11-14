"""Canadian federal law from the Justice Laws XML (``canada_laws``).

Primary tier, free, no key. The act index is reduced and cached; an act's XML is streamed
and only the requested ``Section`` is kept (reading stops as soon as it is found).

Verified (2026-11-14) by three live calls only: the official documentation pages tried
(justice.gc.ca XML page, open.canada.ca dataset) answered 404, so the URLs, the structure
and the licence string (Open Government Licence - Canada) are NOT confirmed by official text:
- ``https://laws-lois.justice.gc.ca/eng/XML/Legis.xml`` (5.3 MB, HTTP 200): ``ActsRegsList``
  with ``Acts/Act`` and ``Regulations/Regulation`` entries, each ``UniqueId``,
  ``OfficialNumber``, ``Language`` (``eng`` | ``fra``: both languages are in the same file),
  ``LinkToXML``, ``Title``, ``CurrentToDate``. Only acts are kept (about 3 900 entries once
  both languages are counted); regulations are not looked up.
- ``https://laws-lois.justice.gc.ca/eng/XML/C-46.xml`` (Criminal Code, 5.8 MB on one line):
  root ``Statute``, ``Body`` with ``Heading`` and ``Section`` siblings, each ``Section``
  having ``Label`` (``319``), ``MarginalNote``, ``Subsection/Label/Text/Paragraph``; the
  attributes ``lims:inforce-start-date`` and ``lims:lastAmendedDate`` give the version dates.
  The French file is under ``/fra/XML/`` (same ids, e.g. ``Code criminel``).
- ``as_of`` is not applied: the XML is the current consolidation (``CurrentToDate`` of the
  index, repeated in the evidence).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from tower.core.adapters.knowledge.http import cached
from tower.core.adapters.knowledge.safe_xml import StreamingXml, UnsafeXml, text_of
from tower.core.domain import error_codes as ec
from tower.core.ports.knowledge import (
    Evidence,
    SourceCache,
    SourceCapabilities,
    SourceQuery,
    SourceUnavailable,
    Tier,
)

__all__ = ["CanadaLawsSource", "match_acts", "section_number"]

NAME = "canada_laws"
LICENSE = "Open Government Licence - Canada"
BASE = "https://laws-lois.justice.gc.ca"
INDEX_MAX_BYTES = 12_000_000
ACT_MAX_BYTES = 40_000_000
MAX_TTL_S = 7 * 86_400
TEXT_CHARS = 2_500

_ID = re.compile(r"^[A-Za-z0-9.\-]{1,40}$")
_ID_IN_TEXT = re.compile(r"\b([A-Z]{1,2}-\d+(?:\.\d+)?)\b")
_SECTION = re.compile(r"\b(?:sections?|s\.|articles?|art\.?)\s*(\d+(?:\.\d+)*)", re.IGNORECASE)


def _norm(value: str) -> str:
    stripped = unicodedata.normalize("NFKD", value.lower().replace("’", "'"))
    return "".join(c for c in stripped if not unicodedata.combining(c))


def section_number(text: str) -> str | None:
    """``section 319`` / ``art. 83.01`` → ``319`` / ``83.01``."""
    match = _SECTION.search(text)
    return match.group(1) if match else None


def match_acts(index: Sequence[Sequence[str]], text: str, lang: str) -> list[list[str]]:
    """Acts of the cached index named in ``text`` (title or ``C-46`` style id), best first.

    Index rows are ``[id, lang, title, current_to]``; the requested language is tried
    first, then the other one. Longest title wins.
    """
    normalized = _norm(text)
    ids = set(_ID_IN_TEXT.findall(text))
    order = (lang, "fra" if lang == "eng" else "eng")
    hits: list[tuple[int, int, list[str]]] = []
    for rank, wanted in enumerate(order):
        for row in index:
            if row[1] != wanted:
                continue
            title = _norm(row[2])
            if row[0] in ids and rank == 0:
                hits.append((rank, -999, list(row)))
            elif len(title) >= 8 and title in normalized:
                hits.append((rank, -len(title), list(row)))
        if hits:
            break
    hits.sort(key=lambda h: (h[0], h[1]))
    return [row for _, _, row in hits][:2]


class CanadaLawsSource:
    name = NAME
    capabilities = SourceCapabilities(
        claim_types=frozenset({"legal_text", "procedure"}), jurisdictions=frozenset({"CA"})
    )
    reliability_tier = Tier.PRIMARY
    cost_per_call_usd_micros = 0
    license = LICENSE

    def __init__(
        self,
        *,
        user_agent: str,
        timeout: float,
        cache: SourceCache | None = None,
        cache_ttl_s: int = MAX_TTL_S,
        transport: Any = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        import httpx

        self._httpx = httpx
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
            headers={"User-Agent": user_agent, "Accept-Encoding": "identity"},
            follow_redirects=True,
            trust_env=transport is None,
        )
        self._cache = cache
        self._ttl = min(cache_ttl_s, MAX_TTL_S)

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- streaming ------------------------------------------------------------------------

    def _fail(self, message: str, code: ec.ErrorCode) -> SourceUnavailable:
        return SourceUnavailable(f"{NAME}: {message}", code=code, source=NAME)

    async def _stream(self, url: str, select: set[str], max_bytes: int) -> Any:
        """Async generator of selected XML elements; closing it closes the HTTP stream."""
        httpx = self._httpx
        parser = StreamingXml(select=select, max_bytes=max_bytes)
        try:
            async with self._client.stream("GET", url) as response:
                status = response.status_code
                if status >= 500:
                    raise self._fail(f"HTTP {status}", ec.ATL_SOURCE_UNAVAILABLE)
                if status >= 400:
                    raise self._fail(f"HTTP {status}", ec.ATL_BAD_RESPONSE)
                async for chunk in response.aiter_bytes(65_536):
                    for element in parser.feed(chunk):
                        yield element
                for element in parser.close():
                    yield element
        except httpx.TimeoutException as exc:
            raise self._fail("timeout", ec.ATL_SOURCE_UNAVAILABLE) from exc
        except httpx.HTTPError as exc:
            raise self._fail(
                f"network error ({type(exc).__name__})", ec.ATL_SOURCE_UNAVAILABLE
            ) from exc
        except UnsafeXml as exc:
            raise self._fail("XML document rejected", ec.ATL_UNSAFE_XML) from exc

    async def _index(self) -> list[list[str]]:
        async def fetch() -> dict[str, Any]:
            rows: list[list[str]] = []
            async for act in self._stream(f"{BASE}/eng/XML/Legis.xml", {"Act"}, INDEX_MAX_BYTES):
                ident = text_of(act.find("UniqueId"))
                lang = text_of(act.find("Language"))
                title = text_of(act.find("Title"))
                if _ID.match(ident) and lang in ("eng", "fra") and title:
                    rows.append([ident, lang, title, text_of(act.find("CurrentToDate"))])
            if not rows:
                raise self._fail("empty act index", ec.ATL_BAD_RESPONSE)
            return {"acts": rows}

        data = await cached(self._cache, NAME, {"op": "index"}, self._ttl, fetch)
        return [list(map(str, r)) for r in data.get("acts", [])]

    async def _section(self, act_id: str, lang: str, number: str) -> dict[str, Any]:
        async def fetch() -> dict[str, Any]:
            url = f"{BASE}/{lang}/XML/{act_id}.xml"
            stream = self._stream(url, {"Section"}, ACT_MAX_BYTES)
            try:
                async for section in stream:
                    if text_of(section.find("Label")) == number:
                        return {
                            "found": True,
                            "text": text_of(section),
                            "inforce_start": section.get("inforce-start-date", ""),
                            "last_amended": section.get("lastAmendedDate", ""),
                        }
            finally:
                await stream.aclose()
            return {"found": False}

        request = {"op": "section", "act": act_id, "lang": lang, "section": number}
        return await cached(self._cache, NAME, request, self._ttl, fetch)

    # --- lookup ---------------------------------------------------------------------------

    async def lookup(self, query: SourceQuery) -> Sequence[Evidence]:
        lang = "fra" if query.lang.lower().startswith("fr") else "eng"
        number = section_number(query.text)
        if number is None and not _ID_IN_TEXT.search(query.text) and len(query.text) < 8:
            return []
        acts = match_acts(await self._index(), query.text, lang)
        out: list[Evidence] = []
        for act_id, act_lang, title, current_to in acts[: max(1, min(query.limit, 2))]:
            if number is None:
                out.append(self._act_evidence(act_id, act_lang, title, current_to))
                continue
            found = await self._section(act_id, act_lang, number)
            if found.get("found"):
                out.append(
                    self._section_evidence(act_id, act_lang, title, current_to, number, found)
                )
        return out

    @staticmethod
    def _url(act_id: str, lang: str) -> str:
        path = "acts" if lang == "eng" else "lois"
        return f"{BASE}/{lang}/{path}/{act_id}/index.html"

    def _act_evidence(self, act_id: str, lang: str, title: str, current_to: str) -> Evidence:
        text = (
            f"{title} ({act_id}; consolidated current to {current_to or 'unknown date'}). "
            "Metadata only: name a section to get its text."
        )
        return self._make(act_id, lang, title, text, current_to, current_to, {})

    def _section_evidence(
        self,
        act_id: str,
        lang: str,
        title: str,
        current_to: str,
        number: str,
        found: dict[str, Any],
    ) -> Evidence:
        since = str(found.get("inforce_start") or "")
        body = str(found.get("text") or "")
        if len(body) > TEXT_CHARS:
            body = body[:TEXT_CHARS].rsplit(" ", 1)[0] + " […]"
        word = "section" if lang == "eng" else "article"
        dates = f"current to {current_to or 'unknown date'}"
        if since:
            dates += f"; this version in force since {since}"
        text = f"{title}, {word} {number} ({dates}): {body}"
        extra = {
            "section": number,
            "in_force_since": since,
            "last_amended": str(found.get("last_amended") or ""),
        }
        return self._make(act_id, lang, title, text, current_to, since or current_to, extra)

    def _make(
        self,
        act_id: str,
        lang: str,
        title: str,
        text: str,
        current_to: str,
        version: str,
        extra: dict[str, str],
    ) -> Evidence:
        published = None
        if current_to:
            try:
                published = datetime.fromisoformat(current_to[:10]).replace(tzinfo=UTC)
            except ValueError:
                published = None
        return Evidence(
            source=NAME,
            stable_id=f"{act_id}:{lang}" + (f":s{extra['section']}" if "section" in extra else ""),
            url=self._url(act_id, lang),
            text=text,
            lang="fr" if lang == "fra" else "en",
            license=LICENSE,
            sha256=hashlib.sha256(text.encode()).hexdigest(),
            published_at=published,
            version=version or None,
            extra={"act": act_id, "current_to": current_to, "as_of_applied": "no", **extra},
            title=title,
            publisher="Department of Justice Canada",
            tier=Tier.PRIMARY,
        )
