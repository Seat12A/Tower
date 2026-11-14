"""World Bank Indicators API (v2): latest non-null value of an indicator for a country.

Primary tier, CC BY 4.0. ``StatIndicator.series`` is the indicator code (``SL.UEM.TOTL.ZS``).

Verified (2026-11-14), datahelpdesk.worldbank.org (About the Indicators API) and a live call:
- ``GET https://api.worldbank.org/v2/country/{ISO3}/indicator/{code}?format=json&mrv=1``:
  HTTP 200, **no key** (docs: "API keys ... are no longer necessary"); rate limits are not
  documented on that page and none was hit.
- Response (live): ``[{"page":1,"pages":1,"per_page":50,"total":1,"sourceid":"2",
  "lastupdated":"2026-11-11"}, [{"indicator":{...},"country":{...},"countryiso3code":"FRA",
  "date":"2025","value":7.542,...}]]``. ``value`` may be null (skipped, ``mrv=1`` can then
  return an empty-valued row: no evidence). ``lastupdated`` is the publication date.
- ``as_of``: ``date=YYYY`` (standard parameter, not exercised live) instead of ``mrv=1``.
- The EU aggregate is requested as ``EUU``; annual data only (``date`` is a year).
- NOT handled: aggregates other than EU, quarterly/monthly series, pagination.
"""

from __future__ import annotations

from typing import Any

from tower.core.adapters.knowledge.stat_common import (
    ISO2_TO_ISO3,
    StatIndicator,
    StatSource,
    normalize_period,
    parse_decimal,
)
from tower.core.domain import error_codes as ec

__all__ = ["WorldBankSource"]

BASE = "https://api.worldbank.org/v2/country"
_EXTRA = {"EU": "EUU"}


class WorldBankSource(StatSource):
    name = "worldbank"
    license = "CC BY 4.0"
    publisher = "World Bank"
    supports_as_of = True

    async def _fetch(
        self, ind: StatIndicator, territory: str, year: int | None
    ) -> dict[str, Any] | None:
        iso3 = ISO2_TO_ISO3.get(territory) or _EXTRA.get(territory)
        if iso3 is None:
            return None
        params = {"format": "json", "date": str(year)} if year else {"format": "json", "mrv": "1"}
        body = await self._http.json("GET", f"{BASE}/{iso3}/indicator/{ind.series}", params=params)
        if not (isinstance(body, list) and len(body) == 2 and isinstance(body[0], dict)):
            raise self._http.fail("worldbank: unexpected response shape", ec.ATL_BAD_RESPONSE)
        rows = body[1]
        if not isinstance(rows, list):
            return None  # e.g. unknown indicator: [{"message": ...}] has no rows
        best: tuple[str, Any] | None = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            value, period = parse_decimal(row.get("value")), str(row.get("date") or "")
            if value is None or normalize_period(period) is None:
                continue
            if best is None or period > best[0]:
                best = (period, value)
        if best is None:
            return None
        return {
            "value": str(best[1]),
            "period": best[0],
            "published": body[0].get("lastupdated"),
            "url": f"https://data.worldbank.org/indicator/{ind.series}",
            "series_key": f"{ind.series}:{iso3}",
        }
