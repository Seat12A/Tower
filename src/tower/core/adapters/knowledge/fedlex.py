"""Swiss federal law through the Fedlex SPARQL endpoint (``fedlex``).

Primary tier, free, no key. Returns the act (title in the query language, SR/RS number,
ELI URI, first entry into force and, when abrogated, the date it ceased to apply); the
act text itself is not stored.

Verified (2026-11-14) by three live calls only: the official pages reachable by fetch
(fedlex.data.admin.ch, fedlex.admin.ch) are JavaScript shells without documentation, so the
endpoint, the JOLux names and the licence string below are NOT confirmed by official text:
- ``GET https://fedlex.data.admin.ch/sparqlendpoint?query=…`` with
  ``Accept: application/sparql-results+json`` answers standard SPARQL JSON (HTTP 200).
- JOLux ``http://data.legilux.public.lu/resource/ontology/jolux#``: acts are
  ``jolux:ConsolidationAbstract`` with ``jolux:classifiedByTaxonomyEntry/skos:notation``
  (the SR number, datatype ``…/notation-type/id-systematique``), ``jolux:isRealizedBy`` an
  expression carrying ``jolux:language`` (``…/authority/language/FRA|DEU|ITA``) and
  ``jolux:title``, ``jolux:dateEntryInForce`` and ``jolux:dateNoLongerInForce``.
- Live: SR ``311.0`` returned ``https://fedlex.data.admin.ch/eli/cc/54/757_781_799``,
  "Code pénal suisse du 21 décembre 1937", entry into force ``1942-01-01``; a German title
  filter returned the Strafgesetzbuch and abrogated acts with a ``dateNoLongerInForce``.
- Caveat: ``dateEntryInForce`` is the first entry into force of the act, not the date of the
  latest consolidated version; ``as_of`` is therefore not applied (``extra["as_of_applied"]``).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
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
    Tier,
)

__all__ = ["FedlexSource", "sr_numbers"]

NAME = "fedlex"
LICENSE = "Open Government Data Switzerland (OGD, terms of use of Fedlex)"
ENDPOINT = "https://fedlex.data.admin.ch/sparqlendpoint"
LANG_URI = "http://publications.europa.eu/resource/authority/language/"
LANGS = {"fr": "FRA", "de": "DEU", "it": "ITA"}
MAX_TTL_S = 7 * 86_400
FETCH_LIMIT = 25

_SR = re.compile(r"\b(?:RS|SR)\s*(\d{1,3}(?:\.\d+)*)\b", re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9äöüéèêàçîô]{4,}")
_STOP = frozenset(
    {
        "suisse", "schweiz", "svizzera", "loi", "gesetz", "legge", "federale", "federal",
        "bundes", "pour", "dans", "avec", "oder", "und", "della", "delle", "nell",
    }
)  # fmt: skip


def sr_numbers(text: str) -> list[str]:
    """SR/RS numbers cited in ``text`` (``RS 311.0``, ``SR 101``)."""
    return list(dict.fromkeys(_SR.findall(text)))[:3]


def _title_words(text: str) -> list[str]:
    lowered = unicodedata.normalize("NFC", text.lower())
    words = [w for w in _WORD.findall(lowered) if w not in _STOP]
    return list(dict.fromkeys(words))[:4]


def _sparql(*, sr: Sequence[str], words: Sequence[str], lang: str) -> str:
    if sr:
        flt = " || ".join(f'STR(?sr) = "{n}"' for n in sr)
    else:
        flt = " && ".join(f'CONTAINS(LCASE(STR(?title)), "{w}")' for w in words)
    return (
        "PREFIX jolux: <http://data.legilux.public.lu/resource/ontology/jolux#>\n"
        "PREFIX skos: <http://www.w3.org/2004/02/skos/core#>\n"
        "SELECT DISTINCT ?act ?sr ?title ?inforce ?abrogated WHERE {\n"
        "  ?act a jolux:ConsolidationAbstract ;\n"
        "       jolux:classifiedByTaxonomyEntry/skos:notation ?sr ;\n"
        "       jolux:isRealizedBy ?expr .\n"
        f"  ?expr jolux:language <{LANG_URI}{lang}> ;\n"
        "        jolux:title ?title .\n"
        "  OPTIONAL { ?act jolux:dateEntryInForce ?inforce }\n"
        "  OPTIONAL { ?act jolux:dateNoLongerInForce ?abrogated }\n"
        f"  FILTER({flt})\n"
        f"}} LIMIT {FETCH_LIMIT}"
    )


def _value(binding: dict[str, Any], key: str) -> str:
    item = binding.get(key)
    return str(item.get("value", "")) if isinstance(item, dict) else ""


class FedlexSource:
    name = NAME
    capabilities = SourceCapabilities(
        claim_types=frozenset({"legal_text", "procedure"}), jurisdictions=frozenset({"CH"})
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
        self._http = SourceHttp(
            NAME, timeout=timeout, user_agent=user_agent, transport=transport, sleep=sleep
        )
        self._cache = cache
        self._ttl = min(cache_ttl_s, MAX_TTL_S)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def lookup(self, query: SourceQuery) -> Sequence[Evidence]:
        code = query.lang.lower()[:2]
        lang_code = code if code in LANGS else "fr"
        sr = sr_numbers(query.text)
        words = [] if sr else _title_words(query.text)
        if not sr and len(words) < 2:
            return []
        sparql = _sparql(sr=sr, words=words, lang=LANGS[lang_code])
        request = {"op": "sparql", "q": sparql}

        async def fetch() -> dict[str, Any]:
            body = await self._http.json_dict(
                "GET",
                ENDPOINT,
                params={"query": sparql},
                headers={"Accept": "application/sparql-results+json"},
            )
            bindings = (body.get("results") or {}).get("bindings")
            if not isinstance(bindings, list):
                raise self._http.fail("fedlex: unexpected SPARQL response", ec.ATL_BAD_RESPONSE)
            return {"bindings": [b for b in bindings if isinstance(b, dict)]}

        data = await cached(self._cache, NAME, request, self._ttl, fetch)
        acts: dict[str, dict[str, Any]] = {}
        for binding in data.get("bindings", []):
            uri = _value(binding, "act")
            if uri and uri not in acts:
                acts[uri] = binding
        # Acts still in force first, then the shortest title (closest to the words asked).
        ordered = sorted(
            acts.items(),
            key=lambda kv: (bool(_value(kv[1], "abrogated")), len(_value(kv[1], "title"))),
        )
        limit = max(1, min(query.limit, 5))
        return [self._evidence(uri, b, lang_code) for uri, b in ordered[:limit]]

    @staticmethod
    def _evidence(uri: str, binding: dict[str, Any], lang: str) -> Evidence:
        title = _value(binding, "title")
        sr = _value(binding, "sr")
        inforce = _value(binding, "inforce")[:10]
        abrogated = _value(binding, "abrogated")[:10]
        parts = [f"SR {sr}" if sr else "SR unknown"]
        parts.append(f"in force since {inforce}" if inforce else "entry into force unknown")
        parts.append(
            f"no longer in force since {abrogated}" if abrogated else "still in force (no end date)"
        )
        text = f"{title} ({'; '.join(parts)}). Metadata only: the text of the act is not stored."
        published = None
        if inforce:
            try:
                published = datetime.fromisoformat(inforce).replace(tzinfo=UTC)
            except ValueError:
                published = None
        return Evidence(
            source=NAME,
            stable_id=uri.removeprefix("https://fedlex.data.admin.ch/eli/"),
            url=uri,
            text=text,
            lang=lang,
            license=LICENSE,
            sha256=hashlib.sha256(text.encode()).hexdigest(),
            published_at=published,
            version=inforce or None,
            extra={
                "sr": sr,
                "eli": uri,
                "in_force_since": inforce,
                "no_longer_in_force_since": abrogated,
                "as_of_applied": "no",
            },
            title=title,
            publisher="Swiss Confederation (Fedlex)",
            tier=Tier.PRIMARY,
        )
