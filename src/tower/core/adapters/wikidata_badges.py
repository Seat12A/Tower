"""Wikidata Query Service (SPARQL) adapter of the badge import. CC0 data.

Checked live on 2026-11-16 against ``https://query.wikidata.org/sparql``:
- the X numeric id (P6552) is stored as a **qualifier** of the X username statement
  (P2002): ~388k statements, against ~100 main P6552 statements. We read the qualifier;
  values are validated as X ids by the caller (some are not numeric);
- one query per scope listing (item, numeric id) is fast (France, citizenship P27: ~12k
  rows in ~7 s; organisations by country P17: ~6.8k rows in ~29 s); adding labels and
  types to it with GROUP BY timed out, so details are fetched by batches of ≤ 200 QIDs
  with ``VALUES`` (< 1 s per batch);
- statements with an end time (P582) or a deprecated rank are ignored.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import unquote

from tower.core.adapters.knowledge.http import SourceHttp
from tower.core.domain import error_codes as ec
from tower.core.domain.badges import WIKIDATA_AFFILIATION_PROPERTIES, is_qid
from tower.core.ports.badges import WikidataAccount, WikidataItem

__all__ = ["SPARQL_ENDPOINT", "WikidataSparqlSource"]

SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = "Seat12A-Tower/0.1 (badge import)"
MAX_DETAIL_QIDS = 200
MAX_HANDLES = 200
INTL_SCOPE = "intl"
_INTL_TYPES = ("Q484652", "Q245065")  # international / intergovernmental organization
_ENTITY = "http://www.wikidata.org/entity/"

_ACCOUNT_FILTER = """
  ?item p:P2002 ?st .
  ?st pq:P6552 ?xid ; wikibase:rank ?rank .
  OPTIONAL { ?st ps:P2002 ?handle }
  FILTER(?rank != wikibase:DeprecatedRank)
  FILTER NOT EXISTS { ?st pq:P582 ?end }
"""


def accounts_queries(scope: str) -> list[str]:
    """SPARQL queries listing (item, numeric id, handle) of a scope."""
    head = "SELECT ?item ?xid ?handle WHERE {"
    if scope == INTL_SCOPE:
        values = " ".join(f"wd:{q}" for q in _INTL_TYPES)
        return [f"{head} VALUES ?t {{ {values} }} ?item wdt:P31 ?t . {_ACCOUNT_FILTER} }}"]
    if not is_qid(scope):
        raise ValueError(f"invalid scope {scope!r}")
    return [
        f"{head} ?item wdt:P27 wd:{scope} . {_ACCOUNT_FILTER} }}",
        f"{head} ?item wdt:P17 wd:{scope} . {_ACCOUNT_FILTER} }}",
    ]


def handles_query(handles: Sequence[str]) -> str:
    """(item, numeric id, handle) of the given X handles (exact strings: WDQS cannot
    compare case-insensitively at this size without timing out; callers pass the case
    variants they want)."""
    values = " ".join(f'"{h}"' for h in dict.fromkeys(handles) if _safe_handle(h))
    return (
        "SELECT ?item ?xid ?handle WHERE { VALUES ?handle { " + values + " } "
        "?item p:P2002 ?st . ?st ps:P2002 ?handle ; pq:P6552 ?xid ; wikibase:rank ?rank . "
        "FILTER(?rank != wikibase:DeprecatedRank) FILTER NOT EXISTS { ?st pq:P582 ?end } }"
    )


def _safe_handle(handle: str) -> bool:
    return 1 <= len(handle) <= 15 and all(c.isascii() and (c.isalnum() or c == "_") for c in handle)


def details_query(qids: Sequence[str]) -> str:
    values = " ".join(f"wd:{q}" for q in qids if is_qid(q))
    links = " ".join(f'(p:{p} ps:{p} "{p}")' for p in WIKIDATA_AFFILIATION_PROPERTIES)
    return f"""SELECT ?item ?k ?v WHERE {{
  VALUES ?item {{ {values} }}
  {{ ?item wdt:P31 ?v . BIND("P31" AS ?k) }}
  UNION {{ ?item wdt:P106 ?v . BIND("P106" AS ?k) }}
  UNION {{ ?item wdt:P154 ?v . BIND("P154" AS ?k) }}
  UNION {{ ?item rdfs:label ?v . FILTER(lang(?v) = "fr") BIND("label" AS ?k) }}
  UNION {{
    VALUES (?prop ?ps ?k) {{ {links} }}
    ?item ?prop ?ls . ?ls ?ps ?v ; wikibase:rank ?r .
    FILTER(?r != wikibase:DeprecatedRank)
    FILTER NOT EXISTS {{ ?ls pq:P582 ?e }}
  }}
}}"""


def _value(binding: Mapping[str, Any], name: str) -> str | None:
    cell = binding.get(name)
    if not isinstance(cell, dict):
        return None
    value = cell.get("value")
    return value if isinstance(value, str) else None


def _qid(uri: str | None) -> str | None:
    if uri is None or not uri.startswith(_ENTITY):
        return None
    q = uri[len(_ENTITY) :]
    return q if is_qid(q) else None


def parse_accounts(body: Mapping[str, Any]) -> list[WikidataAccount]:
    out: list[WikidataAccount] = []
    for b in _bindings(body):
        qid = _qid(_value(b, "item"))
        xid = _value(b, "xid")
        if qid is None or xid is None:
            continue
        out.append(WikidataAccount(qid=qid, x_id=xid.strip(), handle=_value(b, "handle")))
    return out


def parse_details(body: Mapping[str, Any]) -> dict[str, WikidataItem]:
    acc: dict[str, dict[str, Any]] = {}
    for b in _bindings(body):
        qid = _qid(_value(b, "item"))
        key = _value(b, "k")
        value = _value(b, "v")
        if qid is None or key is None or value is None:
            continue
        item = acc.setdefault(
            qid, {"label": None, "types": [], "occupations": [], "logo": None, "links": []}
        )
        if key == "label":
            item["label"] = item["label"] or value
        elif key == "P154":
            # Commons media values come as Special:FilePath URLs; keep the file name.
            item["logo"] = item["logo"] or unquote(value.rsplit("/", 1)[-1])
        elif key in ("P31", "P106"):
            q = _qid(value)
            if q is not None:
                item["types" if key == "P31" else "occupations"].append(q)
        elif key in WIKIDATA_AFFILIATION_PROPERTIES:
            q = _qid(value)
            if q is not None:
                item["links"].append((key, q))
    return {
        qid: WikidataItem(
            qid=qid,
            label=v["label"],
            types=tuple(dict.fromkeys(v["types"])),
            occupations=tuple(dict.fromkeys(v["occupations"])),
            logo=v["logo"],
            links=tuple(dict.fromkeys(v["links"])),
        )
        for qid, v in acc.items()
    }


def _bindings(body: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    results = body.get("results")
    if not isinstance(results, dict):
        return []
    bindings = results.get("bindings")
    return [b for b in bindings if isinstance(b, dict)] if isinstance(bindings, list) else []


class WikidataSparqlSource:
    """Read-only client of the Wikidata Query Service (one httpx client)."""

    def __init__(
        self,
        *,
        endpoint: str = SPARQL_ENDPOINT,
        timeout: float = 70.0,
        transport: Any = None,
    ) -> None:
        self.endpoint = endpoint
        self._http = SourceHttp(
            "wikidata_sparql",
            timeout=timeout,
            user_agent=USER_AGENT,
            transport=transport,
            headers={"Accept": "application/sparql-results+json"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _query(self, query: str) -> dict[str, Any]:
        # POST keeps long VALUES lists out of the URL (documented WDQS form encoding).
        body = await self._http.json_dict("POST", self.endpoint, data={"query": query})
        if not isinstance(body.get("results"), dict):
            raise self._http.fail("wikidata_sparql: unexpected payload", ec.ATL_BAD_RESPONSE)
        return body

    async def x_accounts(self, scope: str) -> Sequence[WikidataAccount]:
        out: list[WikidataAccount] = []
        for query in accounts_queries(scope):
            out.extend(parse_accounts(await self._query(query)))
        return out

    async def accounts_by_handles(self, handles: Sequence[str]) -> Sequence[WikidataAccount]:
        """Accounts whose current X username statement is one of ``handles`` (≤ 200)."""
        if len(handles) > MAX_HANDLES:
            raise ValueError(f"at most {MAX_HANDLES} handles per call")
        if not handles:
            return []
        return parse_accounts(await self._query(handles_query(handles)))

    async def items(self, qids: Sequence[str]) -> Mapping[str, WikidataItem]:
        clean = [q for q in dict.fromkeys(qids) if is_qid(q)]
        if not clean:
            return {}
        if len(clean) > MAX_DETAIL_QIDS:
            raise ValueError(f"at most {MAX_DETAIL_QIDS} QIDs per call")
        return parse_details(await self._query(details_query(clean)))
