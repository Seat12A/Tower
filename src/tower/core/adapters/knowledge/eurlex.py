"""EU legislation through the Publications Office CELLAR SPARQL endpoint (``eurlex``).

Primary tier, free, no key. Only metadata is returned (title, CELEX number, date of the
document, in-force flag, EUR-Lex URL): the act itself is not stored.

Verified (2026-11-14), https://op.europa.eu/en/web/cellar/cellar-data/metadata/knowledge-graph
(confirms the endpoint URL below and the CDM model; that page does not state the HTTP
methods, the result formats or a licence: those come from the live calls; the licence
string is the EU Commission reuse decision, NOT confirmed on that page):
- ``GET https://publications.europa.eu/webapi/rdf/sparql?query=…&format=application/sparql-results+json``
  answers standard SPARQL JSON results (HTTP 200, no credentials; POST is also documented).
- CDM ontology ``http://publications.europa.eu/ontology/cdm#``: ``cdm:resource_legal_id_celex``
  (CELEX literal), ``cdm:work_date_document``, ``cdm:resource_legal_in-force`` (boolean,
  ``"1"`` / ``"0"``), titles on the expression:
  ``?exp cdm:expression_belongs_to_work ?work ; cdm:expression_uses_language
  <http://publications.europa.eu/resource/authority/language/ENG> ; cdm:expression_title ?title``.
- Live: CELEX ``32016R0679`` returned the GDPR title, ``2016-04-27`` and in-force ``1``;
  Virtuoso ``bif:contains`` on the title found Directive (EU) 2019/1937 (``32019L1937``).
- NOT handled: ``as_of`` (the in-force flag is the current one; ``extra["as_of_applied"]``
  says so) and consolidated versions.
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

__all__ = ["EurLexSource", "celex_candidates"]

NAME = "eurlex"
LICENSE = "Reuse of EU law: Commission Decision 2011/833/EU (CC BY 4.0)"
ENDPOINT = "https://publications.europa.eu/webapi/rdf/sparql"
LANG_URI = "http://publications.europa.eu/resource/authority/language/"
LANGS = {"en": "ENG", "fr": "FRA", "de": "DEU", "it": "ITA", "es": "SPA", "nl": "NLD"}
MAX_CELEX = 3
MAX_TTL_S = 7 * 86_400

_CELEX = re.compile(r"\b(3\d{4}[RLDF]\d{4})\b")
_ACT = re.compile(
    r"\b(regulation|reglement|reglamento|verordnung|directive|richtlinie|decision|beschluss)"
    r"\b[^0-9]{0,20}?(?:no\.?\s*)?(\d{1,4})\s*/\s*(\d{1,4})",
    re.IGNORECASE,
)
_TYPE_LETTER = {
    "regulation": "R",
    "reglement": "R",
    "reglamento": "R",
    "verordnung": "R",
    "directive": "L",
    "richtlinie": "L",
    "decision": "D",
    "beschluss": "D",
}
_STOP = frozenset(
    {
        "the", "and", "for", "that", "with", "from", "this", "les", "des", "une", "est",
        "pour", "dans", "que", "qui", "der", "die", "das", "und", "eur", "act", "law",
    }
)  # fmt: skip
_WORD = re.compile(r"[a-z0-9]{4,}")


def _norm(value: str) -> str:
    stripped = unicodedata.normalize("NFKD", value.lower())
    return "".join(c for c in stripped if not unicodedata.combining(c))


def celex_candidates(text: str) -> list[str]:
    """CELEX numbers named in ``text``: explicit ones and "Regulation (EU) 2016/679" forms.

    Since 2015 acts are cited ``year/number`` (``2016/679``); older ones ``number/year``
    (``No 1215/2012``): the group that is a plausible 4-digit year (1957-2100) is the year.
    """
    found: list[str] = list(_CELEX.findall(text))
    for kind, first, second in _ACT.findall(_norm(text)):
        if len(first) == 4 and 1957 <= int(first) <= 2100:
            year, number = first, second
        elif len(second) == 4 and 1957 <= int(second) <= 2100:
            year, number = second, first
        else:
            continue
        found.append(f"3{year}{_TYPE_LETTER[kind.lower()]}{int(number):04d}")
    return list(dict.fromkeys(found))[:MAX_CELEX]


def _sparql_celex(celexes: Sequence[str], lang: str) -> str:
    cond = " || ".join(f'STR(?celex) = "{c}"' for c in celexes)
    return _query(f"FILTER({cond})", "", lang)


def _sparql_title(words: Sequence[str], lang: str) -> str:
    contains = " AND ".join(f"'{w}'" for w in words)
    celex_filter = 'FILTER(REGEX(STR(?celex), "^3[0-9]{4}[RLDF][0-9]{4}$"))'
    return _query(celex_filter, f'?title bif:contains "{contains}" .', lang, restrict_title=True)


def _query(filter_: str, title_filter: str, lang: str, *, restrict_title: bool = False) -> str:
    language = f"<{LANG_URI}{lang}>"
    expression = (
        f"?exp cdm:expression_belongs_to_work ?work ; cdm:expression_uses_language {language} ;"
        " cdm:expression_title ?title ."
    )
    if restrict_title:
        return (
            "PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>\n"
            "SELECT DISTINCT ?celex ?title ?date ?inforce WHERE {\n"
            f"  {expression}\n  {title_filter}\n"
            "  ?work cdm:resource_legal_id_celex ?celex .\n"
            f"  {filter_}\n"
            "  OPTIONAL { ?work cdm:work_date_document ?date }\n"
            "  OPTIONAL { ?work cdm:resource_legal_in-force ?inforce }\n"
            "} LIMIT 10"
        )
    return (
        "PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>\n"
        "SELECT DISTINCT ?celex ?title ?date ?inforce WHERE {\n"
        "  ?work cdm:resource_legal_id_celex ?celex .\n"
        f"  {filter_}\n"
        "  OPTIONAL { ?work cdm:work_date_document ?date }\n"
        "  OPTIONAL { ?work cdm:resource_legal_in-force ?inforce }\n"
        f"  OPTIONAL {{ {expression} }}\n"
        "} LIMIT 10"
    )


def _value(binding: dict[str, Any], key: str) -> str:
    item = binding.get(key)
    return str(item.get("value", "")) if isinstance(item, dict) else ""


class EurLexSource:
    name = NAME
    capabilities = SourceCapabilities(
        claim_types=frozenset({"legal_text", "procedure"}), jurisdictions=frozenset({"EU"})
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
        lang = LANGS.get(query.lang.lower()[:2], "ENG")
        celexes = celex_candidates(query.text)
        if celexes:
            sparql = _sparql_celex(celexes, lang)
        else:
            words = list(
                dict.fromkeys(w for w in _WORD.findall(_norm(query.text)) if w not in _STOP)
            )
            if len(words) < 2:
                return []
            sparql = _sparql_title(words[:5], lang)
        request = {"op": "sparql", "q": sparql}

        async def fetch() -> dict[str, Any]:
            body = await self._http.json_dict(
                "GET",
                ENDPOINT,
                params={"query": sparql, "format": "application/sparql-results+json"},
                headers={"Accept": "application/sparql-results+json"},
            )
            bindings = (body.get("results") or {}).get("bindings")
            if not isinstance(bindings, list):
                raise self._http.fail("eurlex: unexpected SPARQL response", ec.ATL_BAD_RESPONSE)
            return {"bindings": [b for b in bindings if isinstance(b, dict)]}

        data = await cached(self._cache, NAME, request, self._ttl, fetch)
        seen: set[str] = set()
        out: list[Evidence] = []
        for binding in data.get("bindings", []):
            celex = _value(binding, "celex")
            if not celex or celex in seen:
                continue
            seen.add(celex)
            out.append(self._evidence(binding, celex, lang))
            if len(out) >= max(1, min(query.limit, 5)):
                break
        return out

    @staticmethod
    def _evidence(binding: dict[str, Any], celex: str, lang: str) -> Evidence:
        title = _value(binding, "title") or f"EU act {celex}"
        doc_date = _value(binding, "date")
        flag = _value(binding, "inforce").lower()
        in_force = {"1": "yes", "true": "yes", "0": "no", "false": "no"}.get(flag, "unknown")
        text = (
            f"{title} (CELEX {celex}; date of the document {doc_date or 'unknown'}; "
            f"in force: {in_force}). Metadata only: the text of the act is not stored."
        )
        published = None
        if doc_date:
            try:
                published = datetime.fromisoformat(doc_date[:10]).replace(tzinfo=UTC)
            except ValueError:
                published = None
        return Evidence(
            source=NAME,
            stable_id=celex,
            url=f"https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:{celex}",
            text=text,
            lang={"ENG": "en", "FRA": "fr", "DEU": "de", "ITA": "it", "SPA": "es", "NLD": "nl"}[
                lang
            ],
            license=LICENSE,
            sha256=hashlib.sha256(text.encode()).hexdigest(),
            published_at=published,
            version=doc_date or None,
            extra={
                "celex": celex,
                "document_date": doc_date,
                "in_force": in_force,
                "as_of_applied": "no",
            },
            title=title,
            publisher="Publications Office of the European Union",
            tier=Tier.PRIMARY,
        )
