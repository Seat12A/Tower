"""Statistics adapters (Eurostat, World Bank, OECD, IMF) on ``httpx.MockTransport`` and the
daily reference-fact feed against the real database."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
import structlog
from sqlalchemy import select

from tests.conftest import make_settings
from tests.test_atlas_adapters import DictCache, counting
from tower.core.adapters.knowledge.eurostat import EurostatSource
from tower.core.adapters.knowledge.imf import ImfSource
from tower.core.adapters.knowledge.oecd import OecdSource
from tower.core.adapters.knowledge.stat_common import (
    StatIndicator,
    StatValue,
    matching,
    normalize_period,
    parse_stat_indicators,
    period_bounds,
    value_evidence,
)
from tower.core.adapters.knowledge.worldbank import WorldBankSource
from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain import error_codes as ec
from tower.core.factcheck import names
from tower.core.jobs.queue import JobRecord
from tower.core.models.factcheck_v1 import ReferenceFact
from tower.core.models.jobs import Job
from tower.core.ports.knowledge import SourceQuery, SourceUnavailable, Tier
from tower.core.services.base import JobContext
from tower.services.atlas import stats as feed

UA = "Seat12A-Tower/test"
THIS_YEAR = datetime.now(UTC).year


def ind(source: str, series: str, **kw: Any) -> StatIndicator:
    base: dict[str, Any] = {
        "indicator": f"x.{source}",
        "label": "Unemployment rate",
        "source": source,
        "series": series,
        "filters": {},
        "unit": "%",
        "territories": ("FR", "GR", "EU"),
        "definition_variant": "ilo",
        "aliases": ("unemployment rate", "chomage"),
        "period": "year",
        "source_name": source.title(),
    }
    base.update(kw)
    return StatIndicator(**base)


def make(cls: Any, indicator: StatIndicator, handler: Callable[..., Any], **kw: Any) -> Any:
    transport, seen = counting(handler)
    return cls(indicators=[indicator], user_agent=UA, timeout=5, transport=transport, **kw), seen


def json_response(body: Any) -> httpx.Response:
    return httpx.Response(200, json=body)


EUROSTAT_BODY = {
    "version": "2.0",
    "class": "dataset",
    "updated": "2026-11-06T23:00:00+0100",
    "value": {"0": 7.7},
    "id": ["freq", "age", "unit", "sex", "geo", "time"],
    "size": [1, 1, 1, 1, 1, 1],
    "dimension": {"time": {"category": {"index": {"2025": 0}}}},
}
WB_BODY = [
    {"page": 1, "pages": 1, "per_page": 50, "total": 2, "lastupdated": "2026-11-11"},
    [
        {"countryiso3code": "FRA", "date": "2026", "value": None},
        {"countryiso3code": "FRA", "date": "2025", "value": 7.542},
    ],
]
OECD_BODY = {
    "data": {
        "dataSets": [{"observations": {"0:0:0:0:0:0:0:0:0:0": [8.5, None, 0, 0, 0]}}],
        "structure": {
            "dimensions": {
                "observation": [
                    {"id": "REF_AREA", "values": [{"id": "FRA"}]},
                    *[{"id": f"D{i}", "values": [{"id": "x"}]} for i in range(8)],
                    {"id": "TIME_PERIOD", "values": [{"id": "2026-09"}]},
                ]
            }
        },
    }
}
IMF_BODY = {
    "values": {
        "NGDP_RPCH": {"FRA": {"2024": 1.1, "2025": 0.9, str(THIS_YEAR + 2): 1.5, "2023": None}}
    }
}


# --- Eurostat ----------------------------------------------------------------------------


async def test_eurostat_latest_and_territory_mapping() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(EUROSTAT_BODY)

    item = ind("eurostat", "une_rt_a", filters={"unit": "PC_ACT", "sex": "T"})
    src, seen = make(EurostatSource, item, handler)
    value = await src.latest(item.indicator, "GR")
    await src.latest(item.indicator, "EU")
    await src.aclose()
    assert value is not None
    assert (value.value, value.period, value.territory) == (Decimal("7.7"), "2025", "GR")
    assert value.published_at == date(2026, 11, 6)
    assert seen[0].url.params["geo"] == "EL" and seen[1].url.params["geo"] == "EU27_2020"
    assert seen[0].url.params["lastTimePeriod"] == "1" and seen[0].url.params["unit"] == "PC_ACT"
    assert "une_rt_a" in str(seen[0].url)


async def test_eurostat_list_value_quarter_and_as_of() -> None:
    body = {
        **EUROSTAT_BODY,
        "value": [None, 6.4],
        "size": [1, 1, 1, 1, 1, 2],
        "dimension": {"time": {"category": {"index": ["2025Q2", "2025Q3"]}}},
    }
    item = ind("eurostat", "une_rt_q", period="quarter")
    src, seen = make(EurostatSource, item, lambda r: json_response(body))
    value = await src.latest(item.indicator, "FR", datetime(2025, 5, 1, tzinfo=UTC))
    assert value is not None and value.period == "2025-Q3" and value.value == Decimal("6.4")
    assert "time" not in seen[0].url.params  # as_of ignored for a non-annual indicator
    out = await src.lookup(
        SourceQuery(text="unemployment rate", lang="en", jurisdiction="FR", as_of=datetime.now(UTC))
    )
    assert out[0].extra["as_of"] == "ignored: latest value"
    await src.aclose()


async def test_eurostat_as_of_annual_uses_time_param() -> None:
    item = ind("eurostat", "une_rt_a")
    src, seen = make(EurostatSource, item, lambda r: json_response(EUROSTAT_BODY))
    await src.latest(item.indicator, "FR", datetime(2020, 6, 1, tzinfo=UTC))
    assert seen[0].url.params["time"] == "2020" and "lastTimePeriod" not in seen[0].url.params
    await src.aclose()


async def test_eurostat_null_and_empty_give_no_value() -> None:
    item = ind("eurostat", "une_rt_a")
    for body in ({**EUROSTAT_BODY, "value": {}}, {**EUROSTAT_BODY, "value": {"0": None}}):
        src, _ = make(EurostatSource, item, lambda r, b=body: json_response(b))
        assert await src.latest(item.indicator, "FR") is None
        await src.aclose()


# --- World Bank --------------------------------------------------------------------------


async def test_worldbank_skips_null_rows() -> None:
    item = ind("worldbank", "SL.UEM.TOTL.ZS")
    src, seen = make(WorldBankSource, item, lambda r: json_response(WB_BODY))
    value = await src.latest(item.indicator, "FR")
    assert value is not None
    assert (value.value, value.period) == (Decimal("7.542"), "2025")
    assert value.published_at == date(2026, 11, 11)
    assert "/country/FRA/indicator/SL.UEM.TOTL.ZS" in str(seen[0].url)
    assert seen[0].url.params["mrv"] == "1"
    await src.latest(item.indicator, "EU", datetime(2020, 1, 1, tzinfo=UTC))
    assert "/country/EUU/" in str(seen[1].url) and seen[1].url.params["date"] == "2020"
    await src.aclose()


async def test_worldbank_message_only_response_is_no_value() -> None:
    item = ind("worldbank", "BAD.CODE")
    body = [{"message": [{"id": "175", "key": "Invalid format"}]}]
    src, _ = make(WorldBankSource, item, lambda r: json_response(body))
    with pytest.raises(SourceUnavailable) as exc:
        await src.latest(item.indicator, "FR")
    assert exc.value.code == ec.ATL_BAD_RESPONSE
    await src.aclose()


# --- OECD --------------------------------------------------------------------------------


async def test_oecd_latest_period() -> None:
    item = ind("oecd", "OECD.SDD.TPS,DSD_LFS@DF_IALFS_UNE_M,1.0|{geo3}..._Z.Y._T.Y_GE15..M")
    src, seen = make(OecdSource, item, lambda r: json_response(OECD_BODY))
    value = await src.latest(item.indicator, "FR")
    assert value is not None
    assert (value.value, value.period) == (Decimal("8.5"), "2026-09")
    assert value.published_at is None
    assert "/data/OECD.SDD.TPS,DSD_LFS@DF_IALFS_UNE_M,1.0/FRA..._Z.Y._T.Y_GE15..M" in str(
        seen[0].url
    )
    assert seen[0].url.params["lastNObservations"] == "1"
    assert seen[0].url.params["dimensionAtObservation"] == "AllDimensions"
    assert await src.latest(item.indicator, "EU") is None  # no ISO3 for the EU: no call
    assert len(seen) == 1
    await src.aclose()


async def test_oecd_structures_list_and_null_observation() -> None:
    structure: Any = OECD_BODY["data"]["structure"]
    body = {
        "data": {
            "dataSets": [{"observations": {"0:0": [None], "0:1": [3.2]}}],
            "structures": [
                {
                    "dimensions": {
                        "observation": [
                            structure["dimensions"]["observation"][0],
                            {"id": "TIME_PERIOD", "values": [{"id": "2026-Q1"}, {"id": "2025-Q4"}]},
                        ]
                    }
                }
            ],
        }
    }
    item = ind("oecd", "A,B,1.0|{geo3}", period="quarter")
    src, _ = make(OecdSource, item, lambda r: json_response(body))
    value = await src.latest(item.indicator, "FR")
    assert value is not None and (value.period, value.value) == ("2025-Q4", Decimal("3.2"))
    await src.aclose()


# --- IMF ---------------------------------------------------------------------------------


async def test_imf_excludes_projections() -> None:
    item = ind("imf", "NGDP_RPCH")
    src, seen = make(ImfSource, item, lambda r: json_response(IMF_BODY))
    out = await src.lookup(SourceQuery(text="unemployment rate", lang="en", jurisdiction="FR"))
    assert len(out) == 1
    assert out[0].extra["period"] == "2025" and out[0].extra["value"] == "0.9"
    assert out[0].extra["projection"] == "false"
    assert str(seen[0].url).endswith("/api/v1/NGDP_RPCH/FRA")
    await src.aclose()


async def test_imf_unexpected_shape() -> None:
    item = ind("imf", "NGDP_RPCH")
    src, _ = make(ImfSource, item, lambda r: json_response(["nope"]))
    with pytest.raises(SourceUnavailable) as exc:
        await src.latest(item.indicator, "FR")
    assert exc.value.code == ec.ATL_BAD_RESPONSE
    src2, _ = make(ImfSource, item, lambda r: json_response({"values": {}}))
    assert await src2.latest(item.indicator, "FR") is None
    await src.aclose()
    await src2.aclose()


# --- cross-adapter behaviour -------------------------------------------------------------

CASES: list[tuple[Any, StatIndicator, Any]] = [
    (EurostatSource, ind("eurostat", "une_rt_a"), EUROSTAT_BODY),
    (WorldBankSource, ind("worldbank", "SL.UEM.TOTL.ZS"), WB_BODY),
    (OecdSource, ind("oecd", "A,B,1.0|{geo3}"), OECD_BODY),
    (ImfSource, ind("imf", "NGDP_RPCH"), IMF_BODY),
]


@pytest.mark.parametrize(("cls", "item", "body"), CASES)
async def test_cache_hit_makes_no_second_call(cls: Any, item: StatIndicator, body: Any) -> None:
    cache = DictCache()
    src, seen = make(cls, item, lambda r: json_response(body), cache=cache, cache_ttl_s=7 * 86400)
    first = await src.latest(item.indicator, "FR")
    second = await src.latest(item.indicator, "FR")
    assert first is not None and first == second
    assert len(seen) == 1
    assert cache.ttls == [86_400]
    await src.aclose()


@pytest.mark.parametrize(("cls", "item", "body"), CASES)
async def test_http_500_and_malformed_json(cls: Any, item: StatIndicator, body: Any) -> None:
    src, _ = make(cls, item, lambda r: httpx.Response(500))
    with pytest.raises(SourceUnavailable) as exc:
        await src.latest(item.indicator, "FR")
    assert exc.value.code == ec.ATL_SOURCE_UNAVAILABLE
    await src.aclose()
    src, _ = make(cls, item, lambda r: httpx.Response(200, content=b"<html>not json"))
    with pytest.raises(SourceUnavailable) as exc:
        await src.latest(item.indicator, "FR")
    assert exc.value.code == ec.ATL_BAD_RESPONSE
    await src.aclose()


@pytest.mark.parametrize(("cls", "item", "body"), CASES)
async def test_lookup_requires_matching_territory_and_alias(
    cls: Any, item: StatIndicator, body: Any
) -> None:
    src, seen = make(cls, item, lambda r: json_response(body))
    q = SourceQuery(text="Le taux de chômage", lang="fr", jurisdiction="FR")
    assert len(await src.lookup(q)) == 1
    assert await src.lookup(SourceQuery(text="chomage", lang="fr")) == []  # no jurisdiction
    assert await src.lookup(SourceQuery(text="chomage", lang="fr", jurisdiction="US")) == []
    assert await src.lookup(SourceQuery(text="inflation", lang="fr", jurisdiction="FR")) == []
    assert len(seen) == 1
    await src.aclose()


async def test_source_ignores_other_sources_indicators() -> None:
    other = ind("imf", "NGDP_RPCH")
    src, seen = make(EurostatSource, other, lambda r: json_response(EUROSTAT_BODY))
    assert await src.latest(other.indicator, "FR") is None
    assert seen == []
    await src.aclose()


# --- stat_common -------------------------------------------------------------------------


def test_parse_stat_indicators_validation() -> None:
    good = {
        "indicator": "eu.unemployment_rate",
        "label": "Taux de chômage",
        "source": "eurostat",
        "series": "une_rt_a",
        "filters": {"unit": "PC_ACT"},
        "unit": "%",
        "territories": ["fr", "EU"],
        "aliases": ["chomage"],
    }
    assert parse_stat_indicators("") == []
    assert parse_stat_indicators("not json") == []
    assert parse_stat_indicators('{"a": 1}') == []
    raw = json.dumps(
        [
            good,
            {**good, "source": "unknown"},
            {**good, "series": ""},
            {**good, "period": "decade"},
            "x",
        ]
    )
    (item,) = parse_stat_indicators(raw)
    assert item.territories == ("FR", "EU") and item.period == "year"
    assert item.filters == {"unit": "PC_ACT"} and item.source_name == "eurostat"
    assert item.definition_variant is None


def test_matching_picks_best_two_and_checks_territory() -> None:
    a = ind("imf", "A", indicator="a", aliases=("chomage",))
    b = ind("imf", "B", indicator="b", aliases=("taux de chomage", "x y"))
    c = ind("imf", "C", indicator="c", aliases=("chomage",), territories=("US",))
    d = ind("imf", "D", indicator="d", aliases=("chômage",), territories=())
    got = matching([a, b, c, d], "Le TAUX de chômage en France", "fr")
    assert [i.indicator for i in got] == ["b", "a"]
    assert matching([a, c], "chomage", "US") == [c]
    assert matching([a], "chomage", None) == []
    assert matching([d], "chomage", None) == [d]
    assert matching([a], "rien", "FR") == []


def test_period_normalization_and_bounds() -> None:
    assert normalize_period("2025") == "2025"
    assert normalize_period("2025Q3") == "2025-Q3" == normalize_period("2025-Q3")
    assert normalize_period("2025M07") == "2025-07" == normalize_period("2025-07")
    assert normalize_period("2025-M07") == "2025-07"
    assert normalize_period("2025-13") is None and normalize_period("abc") is None
    assert period_bounds("2025") == (date(2025, 1, 1), date(2025, 12, 31))
    assert period_bounds("2025Q3") == (date(2025, 7, 1), date(2025, 9, 30))
    assert period_bounds("2024-02") == (date(2024, 2, 1), date(2024, 2, 29))
    assert period_bounds("x") is None


def test_value_evidence_fields() -> None:
    value = StatValue(
        indicator="eu.u",
        definition_variant="ilo",
        value=Decimal("7.7"),
        unit="%",
        period="2025",
        territory="FR",
        published_at=date(2026, 11, 6),
        source_url="https://example.org/s",
        series_key="une_rt_a:FR:",
    )
    ev = value_evidence("Eurostat", "eurostat", value, title="Unemployment", license="CC BY 4.0")
    assert ev.text == "Unemployment: 7.7 % (2025, FR), published 2026-11-06"
    assert ev.stable_id == "une_rt_a:FR::2025" and ev.tier is Tier.PRIMARY
    assert len(ev.sha256) == 64 and ev.published_at == datetime(2026, 11, 6, tzinfo=UTC)
    assert ev.extra == {
        "indicator": "eu.u",
        "definition_variant": "ilo",
        "value": "7.7",
        "unit": "%",
        "period": "2025",
        "territory": "FR",
        "series_key": "une_rt_a:FR:",
    }
    unknown = value_evidence(
        "E", "e", StatValue("i", None, Decimal(1), "%", "2025", "FR", None, "u", "k"),
        title="T", license="L",
    )  # fmt: skip
    assert unknown.text.endswith("published unknown date") and unknown.published_at is None
    assert unknown.extra["definition_variant"] == ""


# --- feed job ----------------------------------------------------------------------------

INDICATORS = [
    {
        "indicator": "eu.unemployment_rate",
        "label": "Unemployment rate",
        "source": "eurostat",
        "series": "une_rt_a",
        "unit": "%",
        "territories": ["FR", "DE"],
        "definition_variant": "ilo",
        "aliases": ["unemployment rate"],
    },
    {
        "indicator": "no.territory",
        "label": "No territory",
        "source": "imf",
        "series": "X",
        "unit": "%",
    },
]
NOW = datetime.now(UTC)


class FakeStatSource:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    async def latest(self, indicator: str, territory: str, as_of: Any = None) -> StatValue | None:
        self.calls.append((indicator, territory))
        return StatValue(
            indicator=indicator,
            definition_variant="ilo",
            value=Decimal("7.7"),
            unit="%",
            period="2025",
            territory=territory,
            published_at=date(2026, 11, 6),
            source_url="https://ec.europa.eu/eurostat/databrowser/view/une_rt_a/default/table",
            series_key=f"une_rt_a:{territory}:",
        )

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def fake() -> Iterator[FakeStatSource]:
    source = FakeStatSource()

    async def factory(_s: Settings, _d: Database, _i: StatIndicator) -> Any:
        return source

    feed.set_source_factory(factory)
    yield source
    feed.set_source_factory(None)


def job_ctx(db: Database, settings: Settings, payload: dict[str, Any]) -> JobContext:
    job = JobRecord(
        id=1,
        queue=names.ATLAS_QUEUE,
        kind=names.JOB_STAT_REFRESH,
        payload=payload,
        priority=0,
        attempts=1,
        max_attempts=3,
        dedupe_key=None,
        created_at=NOW,
    )
    return JobContext(job=job, db=db, settings=settings, log=structlog.get_logger("test"))


async def facts(db: Database) -> list[ReferenceFact]:
    async with db.session() as s:
        return list((await s.scalars(select(ReferenceFact))).all())


async def test_stat_refresh_creates_one_pending_fact_idempotently(
    db: Database, fake: FakeStatSource
) -> None:
    settings = make_settings(atlas_stat_indicators=json.dumps(INDICATORS))
    ctx = job_ctx(db, settings, {"indicator": "eu.unemployment_rate", "territory": "FR"})
    await feed.stat_refresh(ctx)
    await feed.stat_refresh(ctx)
    (fact,) = await facts(db)
    assert fact.origin == "statistics" and fact.status == "pending"
    assert fact.indicator == "eu.unemployment_rate" and fact.territory == "FR"
    assert fact.value == Decimal("7.7") and fact.unit == "percent"
    assert (fact.period_start, fact.period_end) == (date(2025, 1, 1), date(2025, 12, 31))
    assert fact.external_key == "eurostat:une_rt_a:FR::FR:2025"
    assert fact.definition_variant == "ilo" and fact.published_at == date(2026, 11, 6)
    assert fake.closed and len(fake.calls) == 2


async def test_stat_refresh_unknown_indicator_or_territory_is_noop(
    db: Database, fake: FakeStatSource
) -> None:
    settings = make_settings(atlas_stat_indicators=json.dumps(INDICATORS))
    await feed.stat_refresh(job_ctx(db, settings, {"indicator": "nope", "territory": "FR"}))
    await feed.stat_refresh(
        job_ctx(db, settings, {"indicator": "eu.unemployment_rate", "territory": "US"})
    )
    assert await facts(db) == [] and fake.calls == []


async def test_enqueue_stat_refreshes_one_job_per_territory_and_dedupes(db: Database) -> None:
    settings = make_settings(atlas_stat_indicators=json.dumps(INDICATORS))
    async with db.transaction() as session:
        assert await feed.enqueue_stat_refreshes(session, settings) == 2
    async with db.transaction() as session:
        assert await feed.enqueue_stat_refreshes(session, settings) == 0
    async with db.session() as s:
        jobs = list((await s.scalars(select(Job).where(Job.kind == names.JOB_STAT_REFRESH))).all())
    assert sorted(j.payload["territory"] for j in jobs) == ["DE", "FR"]
    assert {j.queue for j in jobs} == {names.ATLAS_QUEUE}
