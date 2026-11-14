"""Shared plumbing of the statistics adapters (Eurostat, World Bank, OECD, IMF).

These adapters return STRUCTURED values, never prose: the evidence ``extra`` carries
``indicator, definition_variant, value, unit, period, territory, series_key`` as strings.
A tracked indicator is described by a :class:`StatIndicator` (``settings.atlas_stat_indicators``,
a JSON list); a claim matches it by alias words and territory.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from calendar import monthrange
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from tower.core.adapters.knowledge.http import SourceHttp, cached
from tower.core.logging import get_logger
from tower.core.ports.knowledge import (
    Evidence,
    SourceCache,
    SourceCapabilities,
    SourceQuery,
    Tier,
)

__all__ = [
    "ISO2_TO_ISO3",
    "SOURCES",
    "StatIndicator",
    "StatSource",
    "StatValue",
    "matching",
    "normalize_period",
    "parse_decimal",
    "parse_stat_indicators",
    "period_bounds",
    "value_evidence",
]

log = get_logger(__name__)

SOURCES = ("eurostat", "worldbank", "oecd", "imf")
MAX_MATCHES = 2
MAX_BYTES = 2_000_000

ISO2_TO_ISO3: dict[str, str] = {
    "AT": "AUT",
    "BE": "BEL",
    "BG": "BGR",
    "HR": "HRV",
    "CY": "CYP",
    "CZ": "CZE",
    "DK": "DNK",
    "EE": "EST",
    "FI": "FIN",
    "FR": "FRA",
    "DE": "DEU",
    "GR": "GRC",
    "HU": "HUN",
    "IE": "IRL",
    "IT": "ITA",
    "LV": "LVA",
    "LT": "LTU",
    "LU": "LUX",
    "MT": "MLT",
    "NL": "NLD",
    "PL": "POL",
    "PT": "PRT",
    "RO": "ROU",
    "SK": "SVK",
    "SI": "SVN",
    "ES": "ESP",
    "SE": "SWE",
    "GB": "GBR",
    "CH": "CHE",
    "NO": "NOR",
    "US": "USA",
    "CA": "CAN",
    "JP": "JPN",
}
"""EU27 members + GB, CH, NO, US, CA, JP (World Bank, OECD and IMF use ISO 3166-1 alpha-3)."""

_PERIODS = ("year", "quarter", "month")
_YEAR = re.compile(r"^(\d{4})$")
_QUARTER = re.compile(r"^(\d{4})-?Q([1-4])$")
_MONTH = re.compile(r"^(\d{4})-?M?(0[1-9]|1[0-2])$")
_TERRITORY = re.compile(r"^[A-Z]{2}$")


def _norm(text: str) -> str:
    stripped = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in stripped if not unicodedata.combining(c))


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", _norm(text))


@dataclass(frozen=True, slots=True)
class StatIndicator:
    indicator: str
    label: str
    source: str
    series: str
    filters: Mapping[str, str]
    unit: str
    territories: tuple[str, ...]
    definition_variant: str | None
    aliases: tuple[str, ...]
    period: str
    source_name: str


@dataclass(frozen=True, slots=True)
class StatValue:
    indicator: str
    definition_variant: str | None
    value: Decimal
    unit: str
    period: str
    territory: str
    published_at: date | None
    source_url: str
    series_key: str
    extra: Mapping[str, str] = field(default_factory=dict)


def _text(raw: Mapping[str, Any], key: str) -> str | None:
    value = raw.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


_warned_raw: str | None = None


def _warn(raw: str, event: str, **kw: object) -> None:
    global _warned_raw
    if _warned_raw != raw:
        _warned_raw = raw
        log.warning(event, **kw)


def parse_stat_indicators(raw: str) -> list[StatIndicator]:
    """``settings.atlas_stat_indicators`` → indicators. Invalid JSON gives ``[]`` (logged
    once); an invalid entry or an unknown source is skipped."""
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
        if not isinstance(data, list):
            raise ValueError("expected a JSON list")
    except ValueError as exc:
        _warn(raw, "atlas.stat_config_invalid", error=str(exc)[:200])
        return []
    out: list[StatIndicator] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        key, label = _text(entry, "indicator"), _text(entry, "label")
        source, series, unit = _text(entry, "source"), _text(entry, "series"), _text(entry, "unit")
        if not (key and label and source and series and unit) or source not in SOURCES:
            _warn(raw, "atlas.stat_entry_invalid", indicator=key, source=source)
            continue
        filters = entry.get("filters")
        territories = entry.get("territories")
        aliases = entry.get("aliases")
        period = _text(entry, "period") or "year"
        if period not in _PERIODS:
            _warn(raw, "atlas.stat_entry_invalid", indicator=key, period=period)
            continue
        out.append(
            StatIndicator(
                indicator=key,
                label=label,
                source=source,
                series=series,
                filters={str(k): str(v) for k, v in filters.items()}
                if isinstance(filters, dict)
                else {},
                unit=unit,
                territories=tuple(
                    t.strip().upper() for t in territories if isinstance(t, str) and t.strip()
                )
                if isinstance(territories, list)
                else (),
                definition_variant=_text(entry, "definition_variant"),
                aliases=tuple(a for a in aliases if isinstance(a, str))
                if isinstance(aliases, list)
                else (),
                period=period,
                source_name=_text(entry, "source_name") or source,
            )
        )
    return out


def matching(
    indicators: Sequence[StatIndicator], text: str, territory: str | None
) -> list[StatIndicator]:
    """Indicators one alias of which has all its words in ``text`` (accents and case
    ignored) and whose territories contain ``territory``; at most 2, most words first."""
    present = set(_words(text))
    hits: list[tuple[int, StatIndicator]] = []
    for ind in indicators:
        if ind.territories and (territory is None or territory.upper() not in ind.territories):
            continue
        best = 0
        for alias in ind.aliases:
            words = _words(alias)
            if words and all(w in present for w in words):
                best = max(best, len(words))
        if best:
            hits.append((best, ind))
    hits.sort(key=lambda h: -h[0])
    return [ind for _, ind in hits][:MAX_MATCHES]


def normalize_period(raw: str) -> str | None:
    """``2025`` / ``2025-Q3`` / ``2025-07``; accepts ``2025Q3``, ``2025M07``, ``2025-M07``."""
    raw = raw.strip()
    if _YEAR.fullmatch(raw):
        return raw
    if m := _QUARTER.fullmatch(raw):
        return f"{m.group(1)}-Q{m.group(2)}"
    if m := _MONTH.fullmatch(raw):
        return f"{m.group(1)}-{m.group(2)}"
    return None


def period_bounds(period: str) -> tuple[date, date] | None:
    norm = normalize_period(period)
    if norm is None:
        return None
    year = int(norm[:4])
    if len(norm) == 4:
        return date(year, 1, 1), date(year, 12, 31)
    if "Q" in norm:
        first = (int(norm[-1]) - 1) * 3 + 1
        return date(year, first, 1), date(year, first + 2, monthrange(year, first + 2)[1])
    month = int(norm[5:])
    return date(year, month, 1), date(year, month, monthrange(year, month)[1])


def parse_decimal(raw: object) -> Decimal | None:
    """Finite ``Decimal`` from a JSON number or numeric string; ``None`` otherwise."""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def parse_date(raw: object) -> date | None:
    if not isinstance(raw, str) or len(raw) < 10:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def value_evidence(
    source_name: str,
    adapter: str,
    value: StatValue,
    *,
    title: str,
    license: str,
    lang: str = "en",
    extra: Mapping[str, str] | None = None,
) -> Evidence:
    """One deterministic text line; the structured value lives in ``extra``."""
    published = value.published_at.isoformat() if value.published_at else "unknown date"
    text = (
        f"{title}: {value.value} {value.unit} ({value.period}, {value.territory}), "
        f"published {published}"
    )
    fields = {
        "indicator": value.indicator,
        "definition_variant": value.definition_variant or "",
        "value": str(value.value),
        "unit": value.unit,
        "period": value.period,
        "territory": value.territory,
        "series_key": value.series_key,
        **value.extra,
        **(extra or {}),
    }
    return Evidence(
        source=adapter,
        stable_id=f"{value.series_key}:{value.period}",
        url=value.source_url,
        text=text,
        lang=lang,
        license=license,
        sha256=hashlib.sha256(text.encode()).hexdigest(),
        published_at=datetime(
            value.published_at.year, value.published_at.month, value.published_at.day, tzinfo=UTC
        )
        if value.published_at
        else None,
        extra=fields,
        title=title,
        publisher=source_name,
        tier=Tier.PRIMARY,
    )


class StatSource:
    """Base of the four adapters: subclasses set ``name``/``license``/``publisher`` and
    implement :meth:`_fetch` (one HTTP call → a small JSON-able dict, or ``None``)."""

    name = ""
    license = ""
    publisher = ""
    supports_as_of = False
    capabilities = SourceCapabilities(claim_types=frozenset({"statistic"}))
    reliability_tier = Tier.PRIMARY
    cost_per_call_usd_micros = 0

    def __init__(
        self,
        *,
        indicators: Sequence[StatIndicator],
        user_agent: str,
        timeout: float,
        cache: SourceCache | None = None,
        cache_ttl_s: int = 7 * 86_400,
        transport: Any = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._indicators = {i.indicator: i for i in indicators if i.source == self.name}
        self._http = SourceHttp(
            self.name, timeout=timeout, user_agent=user_agent, transport=transport, sleep=sleep
        )
        self._cache = cache
        self._ttl = min(cache_ttl_s, 86_400)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _fetch(
        self, ind: StatIndicator, territory: str, year: int | None
    ) -> dict[str, Any] | None:
        raise NotImplementedError

    def _year(self, ind: StatIndicator, as_of: datetime | None) -> int | None:
        return as_of.year if as_of is not None and self.supports_as_of else None

    async def latest(
        self, indicator: str, territory: str, as_of: datetime | None = None
    ) -> StatValue | None:
        ind = self._indicators.get(indicator)
        territory = territory.upper()
        if ind is None or (ind.territories and territory not in ind.territories):
            return None
        year = self._year(ind, as_of)
        request = {
            "op": "latest",
            "series": ind.series,
            "filters": dict(ind.filters),
            "territory": territory,
            "year": year,
        }

        async def fetch() -> dict[str, Any]:
            return await self._fetch(ind, territory, year) or {"value": None}

        data = await cached(self._cache, self.name, request, self._ttl, fetch)
        value = parse_decimal(data.get("value"))
        period = normalize_period(str(data.get("period") or ""))
        if value is None or period is None:
            log.info("atlas.stat_no_value", source=self.name, indicator=ind.indicator)
            return None
        extra = data.get("extra")
        return StatValue(
            indicator=ind.indicator,
            definition_variant=ind.definition_variant,
            value=value,
            unit=ind.unit,
            period=period,
            territory=territory,
            published_at=parse_date(data.get("published")),
            source_url=str(data.get("url") or ""),
            series_key=str(data.get("series_key") or ind.series),
            extra={str(k): str(v) for k, v in extra.items()} if isinstance(extra, dict) else {},
        )

    async def lookup(self, query: SourceQuery) -> Sequence[Evidence]:
        if query.jurisdiction is None:
            return []
        territory = query.jurisdiction.upper()
        out: list[Evidence] = []
        for ind in matching(list(self._indicators.values()), query.text, territory):
            value = await self.latest(ind.indicator, territory, query.as_of)
            if value is None:
                continue
            extra: dict[str, str] = {}
            if query.as_of is not None and self._year(ind, query.as_of) is None:
                extra["as_of"] = "ignored: latest value"
            out.append(
                value_evidence(
                    self.publisher,
                    self.name,
                    value,
                    title=ind.label,
                    license=self.license,
                    extra=extra,
                )
            )
        return out
