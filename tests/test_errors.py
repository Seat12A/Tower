from __future__ import annotations

from sqlalchemy import select

from tower.core.db.database import Database
from tower.core.domain import error_codes as ec
from tower.core.domain.security import Actor
from tower.core.errors import ErrorReporter
from tower.core.errors import service as errors_service
from tower.core.events.outbox import Cursor, read_events
from tower.core.models.errors import ErrorReport


def _raise_value_error() -> None:
    raise ValueError("boom 1")


async def _reports(db: Database) -> list[ErrorReport]:
    async with db.session() as s:
        return list(await s.scalars(select(ErrorReport)))


async def _topics(db: Database) -> list[str]:
    async with db.session() as s:
        return [e.topic for e in await read_events(s, Cursor())]


async def test_occurrences_are_aggregated_by_fingerprint(db: Database) -> None:
    reporter = ErrorReporter(db, process="test")
    fps = set()
    for i in range(5):
        try:
            _raise_value_error()
        except ValueError as exc:
            fps.add(reporter.capture(ec.INT_UNEXPECTED, exc, message=f"attempt {i}", job_id=i))
    assert len(fps) == 1  # the message varies, the fingerprint does not
    assert await reporter.flush() == 1
    [report] = await _reports(db)
    assert report.occurrences == 5
    assert report.code == "TWR-INT-001"
    assert report.location is None  # only tower.* frames count
    assert report.last_message == "attempt 4"
    assert report.last_traceback and "ValueError" in report.last_traceback
    assert await _topics(db) == ["incident.opened"]

    reporter.capture(ec.INT_UNEXPECTED, KeyError("x"))
    await reporter.flush()
    # different exception type = different fingerprint = new incident
    assert await _topics(db) == ["incident.opened", "incident.opened"]


async def test_non_stored_codes_are_only_logged(db: Database) -> None:
    reporter = ErrorReporter(db, process="test")
    reporter.capture(ec.DOS_NOT_FOUND, message="not found")
    assert await reporter.flush() == 0
    assert await _reports(db) == []


async def test_warnings_are_stored_without_incident(db: Database) -> None:
    reporter = ErrorReporter(db, process="test")
    reporter.capture(ec.JOB_LEASE_EXPIRED, message="2 jobs")
    await reporter.flush()
    assert len(await _reports(db)) == 1
    assert await _topics(db) == []


async def test_resolve_then_reopen(db: Database) -> None:
    reporter = ErrorReporter(db, process="test")
    fp = reporter.capture(ec.MNT_TASK_FAILED, RuntimeError("purge"))
    await reporter.flush()
    async with db.transaction() as s:
        await errors_service.resolve(s, Actor.system("test"), fp, note="fixed")
    [report] = await _reports(db)
    assert report.resolved_at is not None and report.resolved_by == "system:test"

    reporter.capture(ec.MNT_TASK_FAILED, RuntimeError("purge"))
    await reporter.flush()
    [report] = await _reports(db)
    assert report.resolved_at is None
    assert report.occurrences == 2
    assert await _topics(db) == ["incident.opened", "incident.resolved", "incident.reopened"]


async def test_sensitive_context_is_masked(db: Database) -> None:
    reporter = ErrorReporter(db, process="test")
    reporter.capture(ec.INT_UNEXPECTED, message="x", api_token="t12a_secret", user="ok")
    await reporter.flush()
    [report] = await _reports(db)
    assert report.last_context["api_token"] == "[redacted]"
    assert report.last_context["user"] == "ok"


async def test_memory_is_bounded(db: Database) -> None:
    reporter = ErrorReporter(db, process="test", max_pending=2)
    for i in range(5):
        reporter.capture(ec.INT_UNEXPECTED, message=str(i), location_hint=i)
        try:
            raise KeyError(i)
        except KeyError as exc:
            reporter.capture(ec.JOB_EXHAUSTED, exc)
    assert len(reporter._pending) <= 2
