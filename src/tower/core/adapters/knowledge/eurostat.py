"""Eurostat Statistics API (JSON-stat 2.0): latest value of configured datasets.

Primary tier, reuse authorised (CC BY 4.0). ``StatIndicator.series`` is the dataset code,
``filters`` the dimension filters (``unit``, ``sex``, ``age``…).

Verified (2026-11-14), ec.europa.eu/eurostat/web/user-guides/data-browser/api-data-access
and a live call:
- ``GET https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/{dataset}?geo=FR
  &unit=PC_ACT&sex=T&age=Y15-74&lastTimePeriod=1`` (dataset ``une_rt_a``): HTTP 200, no key
  (the docs page does not mention one or any rate limit; none was hit). ``lastTimePeriod=N``
  returns the N latest positions of the time dimension (documented).
- Response (live): JSON-stat 2.0 ``{"version":"2.0","updated":"2026-11-06T23:00:00+0100",
  "value":{"0":7.7},"id":[...],"size":[...],"dimension":{"time":{"category":{"index":{"2025":0}}}}}``;
  with one position per dimension, ``value`` holds flat index ``0``. A list ``value`` and a
  list ``index`` are also accepted (not observed live).
- Geo codes: Greece is ``EL``, the EU is ``EU27_2020`` (mapped from GR / EU).
- ``as_of`` is supported for ANNUAL indicators only (``time=YYYY``, standard Eurostat
  parameter, not exercised live); other frequencies return the latest value and say so.
- NOT handled: flags (``status``), multi-position responses (filters must pin every other
  dimension; a response with several non-time positions yields no value), bulk downloads.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from tower.core.adapters.knowledge.stat_common import (
    MAX_BYTES,
    StatIndicator,
    StatSource,
    normalize_period,
    parse_decimal,
)
from tower.core.domain import error_codes as ec

__all__ = ["EurostatSource"]

BASE = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"
_GEO = {"GR": "EL", "EU": "EU27_2020"}


class EurostatSource(StatSource):
    name = "eurostat"
    license = "Eurostat (reuse authorised, CC BY 4.0)"
    publisher = "Eurostat"
    supports_as_of = True

    def _year(self, ind: StatIndicator, as_of: datetime | None) -> int | None:
        return as_of.year if as_of is not None and ind.period == "year" else None

    async def _fetch(
        self, ind: StatIndicator, territory: str, year: int | None
    ) -> dict[str, Any] | None:
        geo = _GEO.get(territory, territory)
        params: dict[str, str] = {"geo": geo, **ind.filters}
        if year is not None:
            params["time"] = str(year)
        else:
            params["lastTimePeriod"] = "1"
        url = f"{BASE}/{ind.series}"
        response = await self._http.request("GET", url, params=params)
        if len(response.content) > MAX_BYTES:
            raise self._http.fail("eurostat: response too large", ec.ATL_BAD_RESPONSE)
        try:
            body = response.json()
        except ValueError as exc:
            raise self._http.fail("eurostat: response is not JSON", ec.ATL_BAD_RESPONSE) from exc
        if not isinstance(body, dict):
            raise self._http.fail("eurostat: JSON object expected", ec.ATL_BAD_RESPONSE)
        values = body.get("value")
        if isinstance(values, list):
            values = {str(i): v for i, v in enumerate(values) if v is not None}
        if not isinstance(values, dict) or not values:
            return None
        time = (body.get("dimension") or {}).get("time", {}).get("category", {}).get("index")
        if isinstance(time, list):
            time = {str(p): i for i, p in enumerate(time)}
        if not isinstance(time, dict) or not time:
            raise self._http.fail("eurostat: no time dimension", ec.ATL_BAD_RESPONSE)
        size = body.get("size")
        dims = body.get("id")
        if not (isinstance(size, list) and isinstance(dims, list) and "time" in dims):
            raise self._http.fail("eurostat: malformed JSON-stat", ec.ATL_BAD_RESPONSE)
        if any(int(n) != 1 for d, n in zip(dims, size, strict=False) if d != "time"):
            return None  # filters do not pin a single series
        # every other dimension has one position, so the flat index is the time position
        best: tuple[str, Any] | None = None
        for period, pos in time.items():
            raw = values.get(str(int(pos)))
            if parse_decimal(raw) is None or normalize_period(str(period)) is None:
                continue
            if best is None or str(period) > best[0]:
                best = (str(period), raw)
        if best is None:
            return None
        return {
            "value": str(parse_decimal(best[1])),
            "period": best[0],
            "published": body.get("updated"),
            "url": f"https://ec.europa.eu/eurostat/databrowser/view/{ind.series}/default/table",
            "series_key": f"{ind.series}:{geo}:"
            + ",".join(f"{k}={v}" for k, v in ind.filters.items()),
        }
