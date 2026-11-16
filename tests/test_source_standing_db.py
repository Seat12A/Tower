"""Standing of sources with PostgreSQL: identity refresh job, identity lookup and the
first-party rule through the real investigation jobs (fake LLM, fake Atlas, fake
Wikidata)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import structlog
from sqlalchemy import text

from tower.core.adapters.knowledge.atlas import AtlasDocument
from tower.core.adapters.knowledge.wikidata_sites import (
    IDENTITY_CLASSES,
    IdentityRow,
    SubjectSites,
)
from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain import error_codes as ec
from tower.core.factcheck import names, runtime
from tower.core.factcheck.standings import apply_identities, identity_lookup
from tower.core.jobs.queue import JobRecord, PermanentError, RetryLater
from tower.core.ports.knowledge import SourceUnavailable
from tower.core.services.base import JobContext
from tower.services.atlas.identities import enqueue_identity_refreshes, identity_refresh

from .test_investigate_jobs import _request, drain, install, rows, scalar, seed
from .test_investigate_pipeline import FakeAtlas, default_docs


def _ctx(db: Database, settings: Any, payload: dict[str, Any]) -> JobContext:
    record = JobRecord(
        id=1,
        queue=names.ATLAS_QUEUE,
        kind=names.JOB_IDENTITY_REFRESH,
        payload=payload,
        priority=0,
        attempts=1,
        max_attempts=5,
        dedupe_key=None,
        created_at=None,  # type: ignore[arg-type]
    )
    return JobContext(job=record, db=db, settings=settings, log=structlog.get_logger())


def _stream(rows_: list[IdentityRow]) -> Any:
    async def stream(class_qid: str) -> AsyncIterator[IdentityRow]:
        for row in rows_:
            yield row

    return stream


async def test_identity_refresh_upserts_and_keeps_the_higher_kind(
    db: Database, settings: Settings
) -> None:
    press = [
        IdentityRow("lemonde.fr", "Q1", "FR"),
        IdentityRow("lemonde.fr", "Q9", None),  # same host twice in a batch: first wins
        IdentityRow("univ.example", "Q2", None),
    ]
    await identity_refresh(_ctx(db, settings, {"class": "Q11032"}), stream=_stream(press))
    academic = [IdentityRow("univ.example", "Q3", "US")]
    await identity_refresh(_ctx(db, settings, {"class": "Q3918"}), stream=_stream(academic))
    # A lower kind never replaces a higher one.
    await identity_refresh(_ctx(db, settings, {"class": "Q11032"}), stream=_stream(press))
    found = await rows(db, "SELECT domain, qid, kind, country FROM domain_identities ORDER BY 1")
    assert [tuple(r) for r in found] == [
        ("lemonde.fr", "Q1", "press", "FR"),
        ("univ.example", "Q3", "academic", "US"),
    ]
    async with db.session() as session:
        kinds = await identity_lookup(session, ["math.univ.example", "lemonde.fr", "x.org"])
        assert kinds == {"math.univ.example": "academic", "lemonde.fr": "press"}
        docs = [
            AtlasDocument(
                adapter="brave",
                canonical_url="https://math.univ.example/p",
                original_url="https://math.univ.example/p",
                domain="univ.example",
                tier="unrated",
                lang="en",
                text="t",
                tier_basis="default",
            ),
            AtlasDocument(
                adapter="brave",
                canonical_url="https://lemonde.fr/a",
                original_url="https://lemonde.fr/a",
                domain="lemonde.fr",
                tier="secondary",
                lang="fr",
                text="t",
                tier_basis="registry",  # the registry keeps the last word
            ),
        ]
        out = await apply_identities(session, docs)
    assert [(d.tier, d.standing, d.tier_basis) for d in out] == [
        ("secondary", "academic", "identity"),
        ("secondary", None, "registry"),
    ]


async def test_identity_refresh_errors(db: Database, settings: Settings) -> None:
    with pytest.raises(PermanentError):
        await identity_refresh(_ctx(db, settings, {"class": "Q1"}), stream=_stream([]))

    async def broken(class_qid: str) -> AsyncIterator[IdentityRow]:
        yield IdentityRow("a.example", "Q5", None)
        raise SourceUnavailable(
            "wikidata: timeout", code=ec.ATL_SOURCE_UNAVAILABLE, source="wikidata"
        )

    with pytest.raises(RetryLater):
        await identity_refresh(_ctx(db, settings, {"class": "Q11032"}), stream=broken)
    reports = await scalar(db, "SELECT count(*) FROM error_reports WHERE code = 'TWR-ATL-016'")
    assert reports == 1


async def test_enqueue_identity_refreshes_purges_stale_rows(
    db: Database, settings: Settings
) -> None:
    async with db.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO domain_identities (domain, qid, kind, refreshed_at) VALUES "
                "('old.example', 'Q1', 'press', now() - interval '30 days'), "
                "('new.example', 'Q2', 'press', now())"
            )
        )
        queued = await enqueue_identity_refreshes(session, settings)
    every = sum(len(c) for c in IDENTITY_CLASSES.values())
    assert queued == every
    left = await rows(db, "SELECT domain FROM domain_identities")
    assert [r.domain for r in left] == ["new.example"]
    async with db.transaction() as session:
        assert await enqueue_identity_refreshes(session, settings) == 0  # deduplicated


async def test_first_party_and_identity_through_the_jobs(db: Database, settings: Settings) -> None:
    first_party = AtlasDocument(
        adapter="brave",
        canonical_url="https://docs.anthropic.com/pdf",
        original_url="https://docs.anthropic.com/pdf",
        domain="anthropic.com",
        tier="unrated",
        lang="en",
        text="Unemployment rate in France fell to 7 percent in 2025, says the subject's site.",
        title="Docs",
        publisher=None,
        tier_basis="default",
    )
    install(atlas=FakeAtlas([[*default_docs(), first_party]]))
    asked: list[str] = []

    async def sites(qid: str) -> SubjectSites:
        asked.append(qid)
        return SubjectSites(qid, ("anthropic.com",))

    runtime.set_override("subject_sites", sites)
    async with db.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO domain_identities (domain, qid, kind) "
                "VALUES ('anthropic.com', 'Q116758847', 'press')"
            )
        )
    inv_id = await seed(db)
    async with db.transaction() as session:
        await session.execute(text("UPDATE claims SET subject_qid = 'q118876059'"))
    await _request(db, inv_id)
    await drain(db, settings)
    assert asked == ["Q118876059"]
    found = await rows(
        db,
        "SELECT p.standing AS passage_standing, s.tier, s.standing, s.tier_basis "
        "FROM evidence_passages p JOIN sources s ON s.id = p.source_id "
        "WHERE s.domain = 'anthropic.com'",
    )
    assert len(found) == 1
    row = found[0]
    assert row.passage_standing == "first_party"
    assert (row.tier, row.standing, row.tier_basis) == ("unrated", "press", "identity")
    queries = await scalar(db, "SELECT queries FROM investigations WHERE id = :i", i=inv_id)
    assert queries["rounds"][0]["subject_sites"] == ["anthropic.com"]


async def test_subject_sites_failure_does_not_stop_the_investigation(
    db: Database, settings: Settings
) -> None:
    install()

    async def down(qid: str) -> SubjectSites:
        raise SourceUnavailable("wikidata: HTTP 429", code=ec.ATL_RATE_LIMITED, source="wikidata")

    runtime.set_override("subject_sites", down)
    inv_id = await seed(db)
    async with db.transaction() as session:
        await session.execute(text("UPDATE claims SET subject_qid = 'Q1'"))
    await _request(db, inv_id)
    await drain(db, settings)
    assert await scalar(db, "SELECT stage FROM investigations WHERE id = :i", i=inv_id) == (
        "completed"
    )
    assert await scalar(db, "SELECT count(*) FROM error_reports WHERE code = 'TWR-ATL-017'") == 1
