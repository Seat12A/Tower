"""IMF DataMapper API: latest actual (non-projected) value of a WEO indicator.

Primary tier, IMF terms of use. ``StatIndicator.series`` is the DataMapper indicator code
(``NGDP_RPCH``, ``PCPIPCH``…).

Verified (2026-11-14), imf.org/external/datamapper/api/help and a live call:
- Docs: ``GET https://www.imf.org/external/datamapper/api/v1/{indicator}/{ISO3}[?periods=2019,2020]``;
  exact ids come from ``/indicators`` and ``/countries``. The page does not state a key
  requirement or rate limits nor the time-series response shape.
- Live: 2 calls from this sandbox answered **HTTP 403 "Access Denied" (Akamai edge page)**
  with our User-Agent and with an ``Accept: application/json`` header; the response shape
  below is therefore NOT verified live. It is the documented/known DataMapper shape:
  ``{"values": {indicator: {ISO3: {"2024": 7.3, ...}}}}``. This adapter reads it defensively
  (any other shape gives ATL_BAD_RESPONSE, an HTML 403 gives ATL_AUTH_FAILED through
  SourceHttp) and a blocked host simply yields no evidence upstream.
- WEO includes projections: only years <= the current year are returned, and
  ``extra["projection"]="false"`` is set. Years in the current year are still IMF estimates
  for some series; the WEO cut-off year is not exposed by this API (not handled).
- ``as_of``: ``periods=YYYY`` (documented parameter) for the requested year.
- NOT handled: the SDMX 3.0 API (api.imf.org), regional aggregates, EU (no ISO3 code).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from tower.core.adapters.knowledge.stat_common import (
    ISO2_TO_ISO3,
    StatIndicator,
    StatSource,
    normalize_period,
    parse_decimal,
)
from tower.core.domain import error_codes as ec

__all__ = ["ImfSource"]

BASE = "https://www.imf.org/external/datamapper/api/v1"


class ImfSource(StatSource):
    name = "imf"
    license = "IMF terms of use"
    publisher = "IMF"
    supports_as_of = True

    async def _fetch(
        self, ind: StatIndicator, territory: str, year: int | None
    ) -> dict[str, Any] | None:
        iso3 = ISO2_TO_ISO3.get(territory)
        if iso3 is None:
            return None
        params = {"periods": str(year)} if year else None
        body = await self._http.json("GET", f"{BASE}/{ind.series}/{iso3}", params=params)
        try:
            years = body["values"][ind.series][iso3]
        except (KeyError, TypeError) as exc:
            if isinstance(body, dict) and isinstance(body.get("values"), dict):
                return None  # no data for this country/indicator
            raise self._http.fail("imf: unexpected response shape", ec.ATL_BAD_RESPONSE) from exc
        if not isinstance(years, dict):
            raise self._http.fail("imf: unexpected response shape", ec.ATL_BAD_RESPONSE)
        this_year = datetime.now(UTC).year
        best: tuple[str, Any] | None = None
        for period, raw in years.items():
            period = str(period)
            value = parse_decimal(raw)
            if value is None or not (len(period) == 4 and period.isdigit()):
                continue
            if normalize_period(period) is None or int(period) > this_year:
                continue  # projections are never returned
            if best is None or period > best[0]:
                best = (period, value)
        if best is None:
            return None
        return {
            "value": str(best[1]),
            "period": best[0],
            "published": None,
            "url": f"https://www.imf.org/external/datamapper/{ind.series}@WEO/{iso3}",
            "series_key": f"{ind.series}:{iso3}",
            "extra": {"projection": "false"},
        }
