"""Wikidata knowledge source (CC0 1.0): statements with qualifiers and references.

One :class:`Evidence` per selected statement. A statement without a real reference is
``TERTIARY``; with a reference (reference URL ``P854`` or "stated in" ``P248`` …) it is
``SECONDARY`` and the reference URLs go to ``Evidence.references``.

Verified (2026-11-11), Wikidata Action API docs and live calls:
- Entity search: ``GET https://www.wikidata.org/w/api.php?action=wbsearchentities&search=
  &language=&limit=&format=json`` (``language`` required, ``limit`` ≤ 50, ``type=item``
  default) → ``search[]`` with ``id``, ``label``, ``description``, ``concepturi``
  (live-confirmed).
- Entities: ``action=wbgetentities&ids=Q..|Q..&props=claims|labels|info&languages=fr|en
  &format=json&formatversion=2`` (max 50 ids per call). ``claims[P][]`` carry ``id``,
  ``rank``, ``mainsnak{snaktype, datavalue{type, value}}``, ``qualifiers[P][]`` (snaks) and
  ``references[].snaks[P][]`` (data-model page: confirmed; the field-level JSON is the
  documented Wikibase JSON format).
- SPARQL is not used: since May 2025 the graph is split in ``main`` and ``scholarly``
  (federation needed for queries spanning both); the Action API is unaffected.
- NOT confirmed: exact rate limit of the Action API for anonymous clients (the shared
  egress IP was rate-limited during the session); the identified User-Agent is sent.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
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

__all__ = ["WikidataSource"]

NAME = "wikidata"
LICENSE = "CC0 1.0"
API = "https://www.wikidata.org/w/api.php"
SEARCH_TTL_S = 86_400
_QID = re.compile(r"Q[1-9][0-9]{0,15}")
_MAX_IDS = 50
_MAX_EVIDENCE = 30

PROPERTIES: dict[str, str] = {
    "P39": "position held",
    "P1082": "population",
    "P571": "inception",
    "P569": "date of birth",
    "P463": "member of",
    "P6": "head of government",
    "P35": "head of state",
    "P17": "country",
}
"""Selected properties (label in English: stable across requests)."""
_MAX_PER_PROPERTY = {"P39": 8}
_DEFAULT_PER_PROPERTY = 4
QUALIFIERS: dict[str, str] = {
    "P580": "start time",
    "P582": "end time",
    "P585": "point in time",
    "P642": "of",
    "P1545": "series ordinal",
}
# Reference properties that do not make a statement "referenced": bare import markers.
_WEAK_REFERENCE_PROPS = frozenset({"P143", "P4656", "P813", "P3452"})


def _snak_value(snak: Mapping[str, Any]) -> Any | None:
    if snak.get("snaktype") != "value":
        return None
    datavalue = snak.get("datavalue")
    return datavalue.get("value") if isinstance(datavalue, dict) else None


def _format_time(value: Mapping[str, Any]) -> str:
    raw = str(value.get("time", "")).lstrip("+")
    precision = int(value.get("precision", 11) or 11)
    date = raw.split("T")[0]
    if precision <= 9:
        return date[:4] if not date.startswith("-") else date.split("-")[1] + " BCE"
    if precision == 10:
        return date[:7]
    return date


class WikidataSource:
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
        cache_ttl_s: int = 7 * 86_400,
        transport: Any = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._http = SourceHttp(
            NAME, timeout=timeout, user_agent=user_agent, transport=transport, sleep=sleep
        )
        self._cache = cache
        self._ttl = cache_ttl_s

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- API calls -------------------------------------------------------------------

    async def _search(self, text: str, lang: str, limit: int) -> list[str]:
        request = {"op": "search", "q": text, "lang": lang, "limit": limit}

        async def fetch() -> dict[str, Any]:
            body = await self._http.json_dict(
                "GET",
                API,
                params={
                    "action": "wbsearchentities",
                    "search": text,
                    "language": lang,
                    "uselang": lang,
                    "type": "item",
                    "limit": limit,
                    "format": "json",
                },
            )
            hits = body.get("search")
            if not isinstance(hits, list):
                raise self._http.fail("wikidata: search response", ec.ATL_BAD_RESPONSE)
            return {"ids": [str(h["id"]) for h in hits if isinstance(h, dict) and h.get("id")]}

        result = await cached(self._cache, NAME, request, SEARCH_TTL_S, fetch)
        return [i for i in result.get("ids", []) if isinstance(i, str) and _QID.fullmatch(i)]

    async def _entities(
        self, ids: Sequence[str], langs: Sequence[str], *, claims: bool
    ) -> dict[str, dict[str, Any]]:
        langs = list(langs)
        request = {"op": "entities", "ids": sorted(ids), "langs": langs, "claims": claims}

        async def fetch() -> dict[str, Any]:
            body = await self._http.json_dict(
                "GET",
                API,
                params={
                    "action": "wbgetentities",
                    "ids": "|".join(ids),
                    "props": "claims|labels|info" if claims else "labels",
                    "languages": "|".join(langs),
                    "format": "json",
                    "formatversion": 2,
                },
            )
            entities = body.get("entities")
            if not isinstance(entities, dict):
                raise self._http.fail("wikidata: entities response", ec.ATL_BAD_RESPONSE)
            slim: dict[str, Any] = {}
            for qid, entity in entities.items():
                if not isinstance(entity, dict) or "missing" in entity:
                    continue
                item: dict[str, Any] = {"labels": entity.get("labels") or {}}
                if claims:
                    all_claims = entity.get("claims") or {}
                    item["claims"] = {p: all_claims[p] for p in PROPERTIES if p in all_claims}
                    item["lastrevid"] = entity.get("lastrevid")
                slim[str(qid)] = item
            return {"entities": slim}

        result = await cached(self._cache, NAME, request, self._ttl, fetch)
        entities = result.get("entities", {})
        return {k: v for k, v in entities.items() if isinstance(v, dict)}

    # --- lookup ----------------------------------------------------------------------

    async def lookup(self, query: SourceQuery) -> Sequence[Evidence]:
        lang = query.lang.lower()[:12] or "en"
        langs = list(dict.fromkeys([lang, "en", "fr"]))
        if query.subject_qid and _QID.fullmatch(query.subject_qid):
            qids = [query.subject_qid]
        else:
            qids = await self._search(query.text, lang, max(1, min(query.limit, 3)))
        if not qids:
            return []
        entities = await self._entities(qids[:1], langs, claims=True)
        out: list[Evidence] = []
        for qid in qids[:1]:
            entity = entities.get(qid)
            if entity is not None:
                out.extend(await self._evidence(qid, entity, lang, langs))
        return out[:_MAX_EVIDENCE]

    @staticmethod
    def _label(entity: Mapping[str, Any], langs: Sequence[str]) -> str | None:
        labels = entity.get("labels")
        if not isinstance(labels, dict):
            return None
        for lang in langs:
            label = labels.get(lang)
            if isinstance(label, dict) and label.get("value"):
                return str(label["value"])
        return None

    @staticmethod
    def _select(claims: Mapping[str, Any]) -> list[tuple[str, int, dict[str, Any]]]:
        """Non-deprecated statements, preferred rank first, capped per property."""
        picked: list[tuple[str, int, dict[str, Any]]] = []
        for pid in PROPERTIES:
            statements = claims.get(pid)
            if not isinstance(statements, list):
                continue
            usable = [
                s for s in statements if isinstance(s, dict) and s.get("rank") != "deprecated"
            ]
            usable.sort(key=lambda s: 0 if s.get("rank") == "preferred" else 1)
            cap = _MAX_PER_PROPERTY.get(pid, _DEFAULT_PER_PROPERTY)
            picked.extend((pid, i, s) for i, s in enumerate(usable[:cap]))
        return picked

    async def _evidence(
        self, qid: str, entity: Mapping[str, Any], lang: str, langs: Sequence[str]
    ) -> list[Evidence]:
        claims = entity.get("claims")
        if not isinstance(claims, dict):
            return []
        selected = self._select(claims)
        # Entity ids to label (values, qualifier values, "stated in"): one wbgetentities call.
        wanted: dict[str, None] = {}
        for _pid, _i, statement in selected:
            for snak in self._all_snaks(statement):
                value = _snak_value(snak)
                if isinstance(value, dict) and _QID.fullmatch(str(value.get("id", ""))):
                    wanted.setdefault(str(value["id"]), None)
        labels: dict[str, str] = {}
        ids = list(wanted)[:_MAX_IDS]
        if ids:
            resolved = await self._entities(ids, langs, claims=False)
            for entity_id, item in resolved.items():
                label = self._label(item, langs)
                if label:
                    labels[entity_id] = label
        subject = self._label(entity, langs) or qid
        revision = entity.get("lastrevid")
        out: list[Evidence] = []
        for pid, index, statement in selected:
            main_snak = statement.get("mainsnak")
            if not isinstance(main_snak, dict):
                continue
            value = self._render(main_snak, labels)
            if value is None:
                continue
            qualifiers = self._qualifiers(statement, labels)
            ref_urls, stated_in = self._references(statement, labels)
            text = f"{subject} — {PROPERTIES[pid]}: {value}"
            if qualifiers:
                text += f" (qualifiers: {'; '.join(qualifiers)})"
            if ref_urls:
                text += f"; reference: {', '.join(ref_urls)}"
            elif stated_in:
                text += f"; stated in: {', '.join(stated_in)}"
            referenced = bool(ref_urls or stated_in)
            statement_id = statement.get("id")
            out.append(
                Evidence(
                    source=NAME,
                    stable_id=str(statement_id) if statement_id else f"{qid}:{pid}:{index}",
                    url=f"https://www.wikidata.org/wiki/{qid}#{pid}",
                    text=text,
                    lang=lang,
                    license=LICENSE,
                    sha256=hashlib.sha256(text.encode()).hexdigest(),
                    version=str(revision) if revision else None,
                    extra={"qid": qid, "property": pid, "rank": str(statement.get("rank", ""))},
                    title=subject,
                    publisher="Wikidata",
                    tier=Tier.SECONDARY if referenced else Tier.TERTIARY,
                    references=tuple(ref_urls),
                )
            )
        return out

    @staticmethod
    def _all_snaks(statement: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        snaks: list[Mapping[str, Any]] = []
        main = statement.get("mainsnak")
        if isinstance(main, dict):
            snaks.append(main)
        qualifiers = statement.get("qualifiers")
        if isinstance(qualifiers, dict):
            for pid in QUALIFIERS:
                snaks.extend(s for s in qualifiers.get(pid, []) if isinstance(s, dict))
        for reference in statement.get("references") or []:
            if isinstance(reference, dict):
                for snak in (reference.get("snaks") or {}).get("P248", []):
                    if isinstance(snak, dict):
                        snaks.append(snak)
        return snaks

    @staticmethod
    def _render(snak: Mapping[str, Any], labels: Mapping[str, str]) -> str | None:
        value = _snak_value(snak)
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if not isinstance(value, dict):
            return str(value)
        if "id" in value:
            return labels.get(str(value["id"]), str(value["id"]))
        if "time" in value:
            return _format_time(value)
        if "amount" in value:
            return str(value["amount"]).lstrip("+")
        if "text" in value:
            return str(value["text"])
        return None

    def _qualifiers(self, statement: Mapping[str, Any], labels: Mapping[str, str]) -> list[str]:
        qualifiers = statement.get("qualifiers")
        if not isinstance(qualifiers, dict):
            return []
        out: list[str] = []
        for pid, label in QUALIFIERS.items():
            for snak in qualifiers.get(pid, []):
                rendered = self._render(snak, labels) if isinstance(snak, dict) else None
                if rendered is not None:
                    out.append(f"{label} {rendered}")
        return out

    @staticmethod
    def _references(
        statement: Mapping[str, Any], labels: Mapping[str, str]
    ) -> tuple[list[str], list[str]]:
        urls: dict[str, None] = {}
        stated: dict[str, None] = {}
        for reference in statement.get("references") or []:
            snaks = reference.get("snaks") if isinstance(reference, dict) else None
            if not isinstance(snaks, dict):
                continue
            if not any(pid not in _WEAK_REFERENCE_PROPS for pid in snaks):
                continue
            for snak in snaks.get("P854", []):
                value = _snak_value(snak) if isinstance(snak, dict) else None
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    urls.setdefault(value, None)
            for snak in snaks.get("P248", []):
                value = _snak_value(snak) if isinstance(snak, dict) else None
                if isinstance(value, dict) and value.get("id"):
                    stated.setdefault(labels.get(str(value["id"]), str(value["id"])), None)
            if not urls and not stated:
                stated.setdefault("a non-URL reference", None)
        return list(urls), list(stated)
