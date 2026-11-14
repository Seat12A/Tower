"""INSEE reference-fact feed (lot 4), with a fake INSEE source."""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
import structlog
from sqlalchemy import select

from tests.conftest import make_settings
from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain.security import Actor
from tower.core.factcheck import names
from tower.core.jobs.queue import JobRecord, PermanentError, RetryLater
from tower.core.models.errors import ErrorReport
from tower.core.models.factcheck_v1 import ReferenceFact
from tower.core.models.jobs import Job
from tower.core.ports.knowledge import Evidence, SourceQuery
from tower.core.services.base import JobContext
from tower.services.factcheck import reference_feed as feed

NOW = datetime.now(UTC)
INDICATORS = [
    {
        "indicator": "fr.unemployment_rate",
        "label": "Taux de chômage (BIT), France",
        "idbank": "001688527",
        "unit": "percent",
        "territory": "FR",
        "definition_variant": "ilo",
        "subject_qid": None,
        "aliases": ["taux de chomage", "chomage"],
        "period": "quarter",
        "source_name": "INSEE",
    }
]


def settings_for(raw: object) -> Settings:
    return make_settings(fc_insee_indicators=raw if isinstance(raw, str) else json.dumps(raw))


def evidence(period: str = "2026-Q1", value: str = "7.3", *, text: str | None = None) -> Evidence:
    line = text or f"Taux de chômage: {value} Pourcentage ({period}), published 2026-05-15"
    return Evidence(
        source="insee",
        stable_id="001688527",
        url="https://www.insee.fr/fr/statistiques/serie/001688527",
        text=f"{line}\nTaux de chômage: 7.1 Pourcentage (2025-Q4), published 2026-02-15",
        lang="fr",
        license="Licence Ouverte 2.0",
        sha256="0" * 64,
        published_at=datetime(2026, 5, 15, tzinfo=UTC),
        extra={"idbank": "001688527", "periods": f"{period},2025-Q4"},
    )


class FakeSource:
    def __init__(self, result: Sequence[Evidence] | Exception) -> None:
        self.result = result
        self.queries: list[SourceQuery] = []
        self.closed = False

    async def lookup(self, query: SourceQuery) -> Sequence[Evidence]:
        self.queries.append(query)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def source() -> Iterator[FakeSource]:
    fake = FakeSource([evidence()])

    async def factory(_s: Settings, _d: Database, _i: feed.Indicator) -> Any:
        return fake

    feed.set_source_factory(factory)
    yield fake
    feed.set_source_factory(None)


def job_ctx(db: Database, settings: Settings, *, attempts: int = 1) -> JobContext:
    job = JobRecord(
        id=1,
        queue=names.FC_BULK,
        kind=names.JOB_INSEE_REFRESH,
        payload={"indicator": "fr.unemployment_rate"},
        priority=0,
        attempts=attempts,
        max_attempts=3,
        dedupe_key=None,
        created_at=NOW,
    )
    return JobContext(job=job, db=db, settings=settings, log=structlog.get_logger("test"))


async def facts(db: Database) -> list[ReferenceFact]:
    async with db.session() as s:
        return list((await s.scalars(select(ReferenceFact))).all())


def test_parse_indicators_ignores_bad_config() -> None:
    assert feed.parse_indicators("") == []
    assert feed.parse_indicators("not json") == []
    assert feed.parse_indicators('{"a": 1}') == []
    assert feed.parse_indicators(json.dumps([{"indicator": "x"}, 3])) == []
    bad_idbank = [{**INDICATORS[0], "idbank": "12"}]
    assert feed.parse_indicators(json.dumps(bad_idbank)) == []
    (item,) = feed.parse_indicators(json.dumps(INDICATORS))
    assert item.idbank == "001688527" and item.aliases == ("taux de chomage", "chomage")


def test_parse_evidence_periods() -> None:
    assert feed.parse_evidence(evidence("2026-Q2", "8.1")) == (Decimal("8.1"), "2026-Q2")
    assert feed.parse_evidence(evidence("2026-03", "-0.5")) == (Decimal("-0.5"), "2026-03")
    assert feed.parse_evidence(evidence(text="no number here")) is None
    assert feed.parse_evidence(evidence("2026-S1")) is None


async def test_periodic_enqueues_one_job_per_indicator_and_day(
    db: Database, settings: Settings
) -> None:
    conf = settings_for(INDICATORS)
    async with db.transaction() as s:
        assert await feed.enqueue_refreshes(s, conf) == 1
        assert await feed.enqueue_refreshes(s, conf) == 0  # same day: deduplicated
    async with db.session() as s:
        (job,) = (await s.scalars(select(Job).where(Job.kind == names.JOB_INSEE_REFRESH))).all()
    assert job.queue == names.FC_BULK and job.payload == {"indicator": "fr.unemployment_rate"}
    today = datetime.now(UTC).date().isoformat()
    assert job.dedupe_key == names.insee_key("fr.unemployment_rate", today)


async def test_periodic_with_bad_config_does_nothing(db: Database) -> None:
    async with db.transaction() as s:
        assert await feed.enqueue_refreshes(s, settings_for("{broken")) == 0


async def test_refresh_creates_a_pending_fact_once(db: Database, source: FakeSource) -> None:
    conf = settings_for(INDICATORS)
    await feed.insee_refresh(job_ctx(db, conf))
    await feed.insee_refresh(job_ctx(db, conf))
    (fact,) = await facts(db)
    assert fact.status == "pending" and fact.origin == "insee"
    assert fact.external_key == "insee:001688527:2026-Q1"
    assert (fact.period_start, fact.period_end) == (date(2026, 1, 1), date(2026, 3, 31))
    assert str(fact.value) == "7.3" and fact.unit == "percent" and fact.territory == "FR"
    assert fact.definition_variant == "ilo" and fact.published_at == date(2026, 5, 15)
    assert fact.aliases == ["taux de chomage", "chomage"] and fact.next_publication is None
    assert fact.created_by == Actor.system(names.FACTCHECK_ACTOR).label and source.closed
    assert source.queries[0].text == INDICATORS[0]["label"]


async def test_refresh_skips_unparseable_evidence(db: Database, source: FakeSource) -> None:
    source.result = [evidence(text="nothing parseable")]
    await feed.insee_refresh(job_ctx(db, settings_for(INDICATORS)))
    source.result = []
    await feed.insee_refresh(job_ctx(db, settings_for(INDICATORS)))
    assert await facts(db) == []


async def test_refresh_ignores_an_unconfigured_indicator(db: Database, source: FakeSource) -> None:
    await feed.insee_refresh(job_ctx(db, settings_for([])))
    assert source.queries == []


async def test_refresh_failure_is_captured_and_retried(
    db: Database, source: FakeSource, reporter: Any
) -> None:
    source.result = RuntimeError("INSEE down")
    conf = settings_for(INDICATORS)
    with pytest.raises(RetryLater) as exc:
        await feed.insee_refresh(job_ctx(db, conf, attempts=1))
    assert exc.value.delay == 3600
    with pytest.raises(PermanentError):
        await feed.insee_refresh(job_ctx(db, conf, attempts=3))
    await reporter.flush()
    async with db.session() as s:
        reports = list((await s.scalars(select(ErrorReport))).all())
    assert [r.code for r in reports] == ["TWR-FCK-038"]
    assert await facts(db) == []
