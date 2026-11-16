"""Domain identities from Wikidata (job ``atlas.identity_refresh``, weekly).

For each Wikidata class of :data:`IDENTITY_CLASSES` (government agency, university,
scientific journal, newspaper…), streams the entities with an official website (P856)
from the SPARQL endpoint and upserts ``host → (QID, kind, country)`` into
``domain_identities``, in batches (constant memory). One job per class, spaced out so the
public endpoint is not hammered. When several entities claim one host, the highest kind
wins (official > academic > journal > press). Rows not refreshed for 21 days are purged
(entity dissolved, site changed). Not a reliability decision: the human registry always
wins (docs/factcheck/sources.md).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, case, delete, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.adapters.knowledge.http import SourceHttp
from tower.core.adapters.knowledge.wikidata_sites import (
    IDENTITY_CLASSES,
    IdentityRow,
    stream_identities,
)
from tower.core.config import Settings
from tower.core.domain import error_codes as ec
from tower.core.domain.factcheck.standing import IDENTITY_RANK
from tower.core.errors import capture
from tower.core.factcheck import names
from tower.core.jobs.queue import PermanentError, RetryLater, enqueue
from tower.core.models.atlas import DomainIdentity
from tower.core.ports.knowledge import SourceUnavailable
from tower.core.services.base import JobContext

__all__ = ["enqueue_identity_refreshes", "identity_refresh", "kind_of_class", "upsert_identities"]

NAME = "wikidata"
BATCH = 500
SPACING_S = 60
"""Delay between two class jobs of one weekly run (one SPARQL query per minute)."""
STALE_DAYS = 21
RETRY_S = 3600
TIMEOUT_S = 120.0
"""HTTP timeout of the streamed export (the endpoint stops a query after 60 s)."""

IdentityStream = Callable[[str], AsyncIterator[IdentityRow]]


def kind_of_class(class_qid: str) -> str | None:
    for kind, classes in IDENTITY_CLASSES.items():
        if class_qid in classes:
            return str(kind)
    return None


def _rank(column: ColumnElement[str]) -> ColumnElement[int]:
    return case(IDENTITY_RANK, value=column, else_=0)


async def upsert_identities(session: AsyncSession, kind: str, rows: list[IdentityRow]) -> int:
    """Upsert one batch (first row of a host wins inside the batch); an existing row of a
    higher kind is kept. Returns the number of rows sent."""
    unique: dict[str, IdentityRow] = {}
    for row in rows:
        unique.setdefault(row.domain, row)
    if not unique:
        return 0
    stmt = pg_insert(DomainIdentity).values(
        [
            {"domain": r.domain, "qid": r.qid, "kind": kind, "country": r.country}
            for r in unique.values()
        ]
    )
    table = DomainIdentity.__table__
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["domain"],
            set_={
                "qid": stmt.excluded.qid,
                "kind": stmt.excluded.kind,
                "country": stmt.excluded.country,
                "refreshed_at": func.now(),
            },
            where=_rank(stmt.excluded.kind) >= _rank(table.c.kind),
        )
    )
    return len(unique)


async def identity_refresh(ctx: JobContext, *, stream: IdentityStream | None = None) -> None:
    """Job ``atlas.identity_refresh``: payload ``{"class": "Q11032"}``."""
    class_qid = str(ctx.job.payload.get("class", ""))
    kind = kind_of_class(class_qid)
    if kind is None:
        raise PermanentError("atlas.identity_refresh: unknown class")
    http: SourceHttp | None = None
    if stream is None:
        http = SourceHttp(NAME, timeout=TIMEOUT_S, user_agent=ctx.settings.http_user_agent)
        client = http

        def stream(qid: str) -> AsyncIterator[IdentityRow]:
            return stream_identities(client, qid)

    sent = 0
    batch: list[IdentityRow] = []
    try:
        async for row in stream(class_qid):
            batch.append(row)
            if len(batch) >= BATCH:
                async with ctx.db.transaction() as session:
                    sent += await upsert_identities(session, kind, batch)
                batch = []
        if batch:
            async with ctx.db.transaction() as session:
                sent += await upsert_identities(session, kind, batch)
    except SourceUnavailable as exc:
        capture(ec.ATL_IDENTITY_REFRESH_FAILED, exc, wikidata_class=class_qid, rows=sent)
        raise RetryLater(RETRY_S, "wikidata unavailable") from exc
    finally:
        if http is not None:
            await http.aclose()
    ctx.log.info("atlas.identity_refresh.done", wikidata_class=class_qid, kind=kind, rows=sent)


async def enqueue_identity_refreshes(session: AsyncSession, settings: Settings) -> int:
    """Weekly: purge the rows not refreshed for 21 days, then queue one job per class,
    spaced out by one minute. Returns the number of jobs queued."""
    cutoff = datetime.now(UTC) - timedelta(days=STALE_DAYS)
    await session.execute(delete(DomainIdentity).where(DomainIdentity.refreshed_at < cutoff))
    queued = 0
    classes = [c for items in IDENTITY_CLASSES.values() for c in items]
    for i, class_qid in enumerate(classes):
        job = await enqueue(
            session,
            names.ATLAS_QUEUE,
            names.JOB_IDENTITY_REFRESH,
            {"class": class_qid},
            delay=i * SPACING_S,
            dedupe_key=names.identity_refresh_key(class_qid),
        )
        queued += job is not None
    return queued
