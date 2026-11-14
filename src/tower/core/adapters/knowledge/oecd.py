"""OECD Data Explorer API (SDMX REST, SDMX-JSON): latest observation of a dataflow key.

Primary tier, OECD terms (CC BY 4.0). ``StatIndicator.series`` is ``"dataflow|key"``;
``{geo3}`` in the key is replaced by the ISO 3166-1 alpha-3 of the territory, e.g.
``OECD.SDD.TPS,DSD_LFS@DF_IALFS_UNE_M,1.0|{geo3}..._Z.Y._T.Y_GE15..M``.

Verified (2026-11-14), docs PDF on gitlab.algobank.oecd.org (not machine-readable here) and a
live call:
- ``GET https://sdmx.oecd.org/public/rest/data/{dataflow}/{key}?lastNObservations=1
  &dimensionAtObservation=AllDimensions&format=jsondata``: HTTP 200, no key. Rate limits are
  not confirmed from the docs (OECD enforces per-IP limits; a 429 maps to ATL_RATE_LIMITED).
- Response (live, monthly unemployment, France): ``{"meta":{...},"data":{"dataSets":[
  {"observations":{"0:0:0:0:0:0:0:0:0:0":[8.5,null,0,0,0]}}],"structure":{"dimensions":
  {"observation":[{"id":"REF_AREA","values":[{"id":"FRA"}]},...,{"id":"TIME_PERIOD",
  "values":[{"id":"2026-09"}]}]}}}}``. The structure sits under ``data.structure`` (live) or
  ``data.structures[0]`` (SDMX-JSON 2.0): both are read. The key must pin one series; with
  several, the latest TIME_PERIOD among them wins only if they share the value (otherwise no value).
- NOT handled: ``as_of`` (ignored, latest returned), publication date (the response only has
  its preparation time, so ``published_at`` is unknown).
"""

from __future__ import annotations

from typing import Any

from tower.core.adapters.knowledge.stat_common import (
    ISO2_TO_ISO3,
    MAX_BYTES,
    StatIndicator,
    StatSource,
    normalize_period,
    parse_decimal,
)
from tower.core.domain import error_codes as ec

__all__ = ["OecdSource"]

BASE = "https://sdmx.oecd.org/public/rest/data"


class OecdSource(StatSource):
    name = "oecd"
    license = "OECD terms (CC BY 4.0)"
    publisher = "OECD"

    async def _fetch(
        self, ind: StatIndicator, territory: str, year: int | None
    ) -> dict[str, Any] | None:
        iso3 = ISO2_TO_ISO3.get(territory)
        dataflow, sep, key = ind.series.partition("|")
        if iso3 is None or not sep:
            return None
        params = {
            "lastNObservations": "1",
            "dimensionAtObservation": "AllDimensions",
            "format": "jsondata",
        }
        url = f"{BASE}/{dataflow}/{key.replace('{geo3}', iso3)}"
        response = await self._http.request("GET", url, params=params)
        if len(response.content) > MAX_BYTES:
            raise self._http.fail("oecd: response too large", ec.ATL_BAD_RESPONSE)
        try:
            body = response.json()
        except ValueError as exc:
            raise self._http.fail("oecd: response is not JSON", ec.ATL_BAD_RESPONSE) from exc
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            raise self._http.fail("oecd: unexpected response shape", ec.ATL_BAD_RESPONSE)
        structure: Any = data.get("structure")
        if structure is None and isinstance(data.get("structures"), list) and data["structures"]:
            structure = data["structures"][0]
        datasets: Any = data.get("dataSets")
        try:
            dims = structure["dimensions"]["observation"]
            observations = datasets[0]["observations"]
            ids = [str(d["id"]) for d in dims]
            time_pos = ids.index("TIME_PERIOD")
            time_values = [str(v["id"]) for v in dims[time_pos]["values"]]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise self._http.fail("oecd: malformed SDMX-JSON", ec.ATL_BAD_RESPONSE) from exc
        if not isinstance(observations, dict):
            raise self._http.fail("oecd: malformed SDMX-JSON", ec.ATL_BAD_RESPONSE)
        flow_id = dataflow.split(",")[1] if "," in dataflow else dataflow
        series_positions: set[str] = set()
        best: tuple[str, Any] | None = None
        for obs_key, cells in observations.items():
            parts = str(obs_key).split(":")
            try:
                period = time_values[int(parts[time_pos])]
            except (IndexError, ValueError):
                continue
            series_positions.add(":".join(p for i, p in enumerate(parts) if i != time_pos))
            value = parse_decimal(cells[0]) if isinstance(cells, list) and cells else None
            if value is None or normalize_period(period) is None:
                continue
            if best is None or period > best[0]:
                best = (period, value)
        if best is None or len(series_positions) != 1:
            return None
        return {
            "value": str(best[1]),
            "period": best[0],
            "published": None,
            "url": f"https://data-explorer.oecd.org/?lc=en&df[id]={flow_id}",
            "series_key": f"{dataflow}:{key.replace('{geo3}', iso3)}",
        }
