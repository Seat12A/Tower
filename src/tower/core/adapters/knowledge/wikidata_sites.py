"""Official websites from Wikidata (property P856), for the standing of sources.

Two uses (docs/factcheck/sources.md):

- :func:`make_subject_sites`: official sites of a claim's subject and of the entities
  behind it (developer, manufacturer, publisher, operator, parent organization, owner):
  Claude (Q…) → Anthropic → ``anthropic.com``. Two cached Action API calls
  (``wbgetentities``) at most; never a search by name (a homonym would give a wrong site);
- :func:`stream_identities`: every entity of one class with an official website, streamed
  from the SPARQL endpoint as TSV (constant memory, one request per class), for the
  ``domain_identities`` table.

httpx is only loaded through :class:`SourceHttp`.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from tower.core.adapters.knowledge.http import SourceHttp, cached
from tower.core.domain import error_codes as ec
from tower.core.domain.factcheck.standing import IdentityKind, is_shared_host
from tower.core.ports.knowledge import SourceCache

__all__ = [
    "IDENTITY_CLASSES",
    "IdentityRow",
    "SubjectSites",
    "make_subject_sites",
    "parse_identity_line",
    "site_host",
    "stream_identities",
]

API = "https://www.wikidata.org/w/api.php"
SPARQL = "https://query.wikidata.org/sparql"
NAME = "wikidata"
OFFICIAL_WEBSITE = "P856"
RELATED = ("P178", "P176", "P123", "P137", "P749", "P127")
"""Developer, manufacturer, publisher, operator, parent organization, owned by."""
MAX_RELATED = 10
_QID = re.compile(r"^Q[1-9][0-9]{0,15}$")
_ISO2 = re.compile(r"^[A-Z]{2}$")

IDENTITY_CLASSES: dict[str, tuple[str, ...]] = {
    IdentityKind.OFFICIAL: (
        "Q327333",  # government agency
        "Q192350",  # ministry
        "Q7188",  # government
        "Q11204",  # legislature
        "Q35749",  # parliament
        "Q41487",  # court
        "Q190752",  # supreme court
        "Q32766",  # constitutional court
        "Q245065",  # intergovernmental organization
        "Q66344",  # central bank
        "Q2659904",  # government organization
        "Q1639780",  # regulatory agency
        "Q17505024",  # space agency
        "Q480242",  # statistical service
        "Q935741",  # election commission
    ),
    IdentityKind.ACADEMIC: (
        "Q3918",  # university
        "Q875538",  # public university
        "Q902104",  # private university
        "Q3354859",  # collegiate university
        "Q31855",  # research institute
        "Q3726309",  # public research institution
        "Q414147",  # academy of sciences
        "Q955824",  # learned society
    ),
    IdentityKind.JOURNAL: (
        "Q737498",  # academic journal
        "Q5633421",  # scientific journal
    ),
    IdentityKind.PRESS: (
        "Q11032",  # newspaper
        "Q1110794",  # daily newspaper
        "Q1153191",  # online newspaper
        "Q2305295",  # weekly newspaper
        "Q17232649",  # news website
        "Q192283",  # news agency
        "Q1126006",  # public broadcaster
        "Q15265344",  # broadcaster
        "Q1684600",  # news magazine
    ),
}
"""Wikidata classes (direct ``P31``) per identity kind; labels checked on 2026-11-16."""

_IDENTITY_QUERY = """SELECT ?item ?site ?iso WHERE {
  ?item wdt:P31 wd:%s ; wdt:P856 ?site .
  FILTER NOT EXISTS { ?item wdt:P576 [] }
  OPTIONAL { ?item wdt:P17/wdt:P297 ?iso }
}"""
"""Entities of one class with an official website, not dissolved (P576), with the ISO
code of their country (P17/P297) when known."""


def site_host(url: str) -> str | None:
    """Host of an official website (lowercase, no ``www.``), ``None`` when it is not an
    http(s) URL or lies on a shared hosting platform."""
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or "." not in host:
        return None
    host = host.removeprefix("www.")
    if is_shared_host(host):
        return None
    return host


@dataclass(frozen=True, slots=True)
class IdentityRow:
    domain: str
    qid: str
    country: str | None


def parse_identity_line(line: str) -> IdentityRow | None:
    """One TSV line ``<…/entity/Q1>\\t<https://site/>\\t"FR"`` → row (``None`` for the
    header, a malformed line or an unusable site)."""
    cells = line.rstrip("\r\n").split("\t")
    if len(cells) < 2 or not cells[0].startswith("<"):
        return None
    qid = cells[0].strip("<>").rsplit("/", 1)[-1]
    if not _QID.fullmatch(qid):
        return None
    host = site_host(cells[1].strip("<>"))
    if host is None:
        return None
    iso = cells[2].strip().strip('"').upper() if len(cells) > 2 else ""
    return IdentityRow(domain=host, qid=qid, country=iso if _ISO2.fullmatch(iso) else None)


async def stream_identities(http: SourceHttp, class_qid: str) -> AsyncIterator[IdentityRow]:
    """Rows of one class (no cache: a weekly refresh of a public export)."""
    if not _QID.fullmatch(class_qid):
        raise ValueError(f"not a QID: {class_qid!r}")
    async for line in http.lines(
        SPARQL,
        params={"query": _IDENTITY_QUERY % class_qid},
        headers={"Accept": "text/tab-separated-values"},
    ):
        row = parse_identity_line(line)
        if row is not None:
            yield row


@dataclass(frozen=True, slots=True)
class SubjectSites:
    qid: str
    hosts: tuple[str, ...]
    """Official hosts of the subject and of the entities behind it (subject first)."""


SubjectSitesFetch = Callable[[str], Awaitable[SubjectSites]]


def _values(entity: Mapping[str, Any], prop: str) -> list[Any]:
    out: list[Any] = []
    for claim in (entity.get("claims") or {}).get(prop, []):
        if not isinstance(claim, dict) or claim.get("rank") == "deprecated":
            continue
        if "P582" in (claim.get("qualifiers") or {}):  # end time: no longer true
            continue
        snak = claim.get("mainsnak")
        if not isinstance(snak, dict) or snak.get("snaktype") != "value":
            continue
        out.append((snak.get("datavalue") or {}).get("value"))
    return out


def _hosts(entity: Mapping[str, Any]) -> list[str]:
    hosts = [site_host(v) for v in _values(entity, OFFICIAL_WEBSITE) if isinstance(v, str)]
    return [h for h in hosts if h]


def make_subject_sites(
    http: SourceHttp, cache: SourceCache | None, ttl_s: int
) -> SubjectSitesFetch:
    """Default fetcher: the subject's claims, then the claims of the related entities."""

    async def claims(ids: list[str]) -> dict[str, Any]:
        request = {"op": "sites", "ids": sorted(ids)}

        async def fetch() -> dict[str, Any]:
            body = await http.json_dict(
                "GET",
                API,
                params={
                    "action": "wbgetentities",
                    "ids": "|".join(sorted(ids)),
                    "props": "claims",
                    "format": "json",
                    "formatversion": 2,
                },
            )
            found = body.get("entities")
            if not isinstance(found, dict):
                raise http.fail("wikidata: entities response", ec.ATL_BAD_RESPONSE)
            slim: dict[str, Any] = {}
            for qid, ent in found.items():
                if not isinstance(ent, dict) or "missing" in ent:
                    continue
                kept = ent.get("claims") or {}
                slim[str(qid)] = {
                    "claims": {p: kept[p] for p in (OFFICIAL_WEBSITE, *RELATED) if p in kept}
                }
            return {"entities": slim}

        result = await cached(cache, NAME, request, ttl_s, fetch)
        out = result.get("entities", {})
        return out if isinstance(out, dict) else {}

    async def subject_sites(qid: str) -> SubjectSites:
        if not _QID.fullmatch(qid):
            return SubjectSites(qid, ())
        entity = (await claims([qid])).get(qid)
        if not isinstance(entity, dict):
            return SubjectSites(qid, ())
        hosts = _hosts(entity)
        related: list[str] = []
        for prop in RELATED:
            for value in _values(entity, prop):
                target = value.get("id") if isinstance(value, dict) else None
                fresh = isinstance(target, str) and target not in related and target != qid
                if fresh and isinstance(target, str) and _QID.fullmatch(target):
                    related.append(target)
        if related:
            found = await claims(related[:MAX_RELATED])
            for target in related[:MAX_RELATED]:
                ent = found.get(target)
                if isinstance(ent, dict):
                    hosts.extend(_hosts(ent))
        return SubjectSites(qid, tuple(dict.fromkeys(hosts)))

    return subject_sites
