"""INSEE BDM (SDMX web service): latest observations of configured series.

Primary tier, Licence Ouverte 2.0. Series come from ``settings.insee_series``
(``{"indicator keyword": "idbank"}``); a series is looked up when all the words of its
keyword appear in the query text (case and accents ignored).

Verified (2026-11-11), insee.fr/fr/information/2862759 and a live call:
- ``GET https://bdm.insee.fr/series/sdmx/data/SERIES_BDM/{idbank}[+{idbank}…]?lastNObservations=N``
  (up to 400 idbanks, ``startPeriod``, ``updatedAfter``, ``detail`` also exist); free of
  charge and **no key required** on this host (live call answered 200 without credentials;
  the ``insee_api_key`` setting is therefore not sent). The api.insee.fr portal route
  (authenticated) is not used.
- Response: SDMX-ML 2.1 StructureSpecificData, ``<Series IDBANK TITLE_FR TITLE_EN LAST_UPDATE
  UNIT_MEASURE UNIT_MULT FREQ REF_AREA DECIMALS>`` with ``<Obs TIME_PERIOD OBS_VALUE
  OBS_STATUS …/>`` children, newest first (live-confirmed; the sample series 001688370 is a
  stopped series). Attributes are read as attributes whatever the namespace.
- NOT handled: ``as_of`` (the period filter format varies by frequency and was not
  confirmed): the evidence is the latest observations and says so in ``extra["as_of"]``.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable, Mapping, Sequence
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

__all__ = ["InseeSource", "parse_series_config"]

NAME = "insee"
LICENSE = "Licence Ouverte 2.0"
BASE = "https://bdm.insee.fr/series/sdmx/data/SERIES_BDM"
LAST_N = 4
MAX_SERIES = 2
MAX_BYTES = 1_000_000
_IDBANK = re.compile(r"[0-9]{9}")


def _norm(text: str) -> str:
    stripped = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in stripped if not unicodedata.combining(c))


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", _norm(text))


def parse_series_config(raw: str) -> dict[str, str]:
    """``settings.insee_series`` → ``{keyword: idbank}``; invalid entries are dropped."""
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if _IDBANK.fullmatch(str(v))}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class InseeSource:
    name = NAME
    capabilities = SourceCapabilities(
        claim_types=frozenset({"statistic"}), jurisdictions=frozenset({"FR"})
    )
    reliability_tier = Tier.PRIMARY
    cost_per_call_usd_micros = 0
    license = LICENSE

    def __init__(
        self,
        *,
        series: Mapping[str, str],
        user_agent: str,
        timeout: float,
        cache: SourceCache | None = None,
        cache_ttl_s: int = 7 * 86_400,
        transport: Any = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._series = dict(series)
        self._http = SourceHttp(
            NAME, timeout=timeout, user_agent=user_agent, transport=transport, sleep=sleep
        )
        self._cache = cache
        self._ttl = min(cache_ttl_s, 86_400)  # statistics move at publication dates

    async def aclose(self) -> None:
        await self._http.aclose()

    def matching(self, text: str) -> list[str]:
        """idbanks whose keyword words all appear in ``text`` (most words first)."""
        present = set(_words(text))
        hits: list[tuple[int, str]] = []
        for keyword, idbank in self._series.items():
            words = _words(keyword)
            if words and all(w in present for w in words):
                hits.append((len(words), idbank))
        hits.sort(key=lambda h: -h[0])
        return list(dict.fromkeys(i for _, i in hits))[:MAX_SERIES]

    async def lookup(self, query: SourceQuery) -> Sequence[Evidence]:
        out: list[Evidence] = []
        for idbank in self.matching(query.text):
            evidence = await self._series_evidence(idbank, query)
            if evidence is not None:
                out.append(evidence)
        return out

    async def _series_evidence(self, idbank: str, query: SourceQuery) -> Evidence | None:
        request = {"op": "series", "idbank": idbank, "last": LAST_N}

        async def fetch() -> dict[str, Any]:
            response = await self._http.request(
                "GET",
                f"{BASE}/{idbank}",
                params={"lastNObservations": LAST_N},
                headers={"Accept": "application/xml"},
            )
            if len(response.content) > MAX_BYTES:
                raise self._http.fail("insee: response too large", ec.ATL_BAD_RESPONSE)
            return self._parse(response.content, idbank)

        data = await cached(self._cache, NAME, request, self._ttl, fetch)
        observations = data.get("observations")
        if not isinstance(observations, list) or not observations:
            return None
        title = str(data.get("title") or idbank)
        unit = str(data.get("unit") or "").strip()
        published = str(data.get("last_update") or "unknown date")
        lines = [
            f"{title}: {obs['value']}{(' ' + unit) if unit else ''} ({obs['period']}), "
            f"published {published}"
            for obs in observations
            if isinstance(obs, dict)
        ]
        text = "\n".join(lines)
        published_at: datetime | None = None
        try:
            published_at = datetime.fromisoformat(published).replace(tzinfo=UTC)
        except ValueError:
            published_at = None
        extra = {"idbank": idbank, "periods": ",".join(str(o["period"]) for o in observations)}
        if query.as_of is not None:
            extra["as_of"] = "ignored: latest observations returned"
        return Evidence(
            source=NAME,
            stable_id=idbank,
            url=f"https://www.insee.fr/fr/statistiques/serie/{idbank}",
            text=text,
            lang="fr",
            license=LICENSE,
            sha256=hashlib.sha256(text.encode()).hexdigest(),
            published_at=published_at,
            version=published if published_at else None,
            extra=extra,
            title=title,
            publisher="INSEE",
            tier=Tier.PRIMARY,
        )

    def _parse(self, content: bytes, idbank: str) -> dict[str, Any]:
        try:
            root = ET.fromstring(content)  # noqa: S314
        except ET.ParseError as exc:
            raise self._http.fail("insee: response is not XML", ec.ATL_BAD_RESPONSE) from exc
        series = next((e for e in root.iter() if _local(e.tag) == "Series"), None)
        if series is None:
            return {"observations": []}
        observations: list[dict[str, str]] = []
        for obs in series:
            if _local(obs.tag) != "Obs":
                continue
            value = obs.attrib.get("OBS_VALUE")
            period = obs.attrib.get("TIME_PERIOD")
            if value is None or period is None:
                continue
            observations.append({"period": period, "value": value})
        observations.sort(key=lambda o: o["period"], reverse=True)
        attrs = series.attrib
        unit = attrs.get("UNIT_MEASURE", "")
        mult = attrs.get("UNIT_MULT", "0")
        if unit and mult not in ("", "0"):
            unit = f"{unit} x10^{mult}"
        return {
            "title": attrs.get("TITLE_FR") or attrs.get("TITLE_EN") or idbank,
            "unit": unit,
            "last_update": attrs.get("LAST_UPDATE", ""),
            "observations": observations[:LAST_N],
        }
