from __future__ import annotations

import asyncio
import contextlib
import hashlib
import traceback
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any

import orjson
import structlog
from sqlalchemy import select, text

from tower.core.domain.error_codes import ErrorCode, Severity
from tower.core.logging import get_logger

log = get_logger("tower.errors")

_MAX_TRACEBACK = 8_000
_MAX_MESSAGE = 2_000
_SENSITIVE = ("token", "authorization", "password", "secret", "api_key", "cookie")

_LOG_METHOD = {
    Severity.INFO: "info",
    Severity.WARNING: "warning",
    Severity.ERROR: "error",
    Severity.CRITICAL: "critical",
}


@dataclass(slots=True)
class _Pending:
    code: ErrorCode
    location: str | None
    exception_type: str | None
    message: str | None
    traceback: str | None
    context: dict[str, Any]
    count: int = 1
    first_ts: float = field(default=0.0)


def _location(tb: TracebackType | None) -> str | None:
    """Last frame belonging to Tower's code: ``module:function``."""
    found = None
    while tb is not None:
        module = tb.tb_frame.f_globals.get("__name__", "")
        if module.startswith("tower."):
            found = f"{module}:{tb.tb_frame.f_code.co_name}"
        tb = tb.tb_next
    return found


def fingerprint(code: ErrorCode, exception_type: str | None, location: str | None) -> str:
    """Stable fingerprint: the (variable) message is not part of it."""
    raw = f"{code.code}|{exception_type or ''}|{location or ''}"
    return hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest()[:16]


def _scrub(context: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in context.items():
        if any(s in key.lower() for s in _SENSITIVE):
            clean[key] = "[redacted]"
        elif isinstance(value, str | int | float | bool) or value is None:
            clean[key] = value
        else:
            clean[key] = str(value)[:500]
    return clean


class ErrorReporter:
    """Per-process error buffer, periodically flushed to the database.

    Bounded memory: at most ``max_pending`` distinct fingerprints between two flushes;
    beyond that, new fingerprints are counted as lost (reported in the logs), but
    occurrences of known fingerprints keep being aggregated.
    """

    def __init__(
        self,
        db: Any = None,
        *,
        process: str = "tower",
        flush_interval: float = 5.0,
        max_pending: int = 500,
    ) -> None:
        self.db = db
        self.process = process
        self.flush_interval = flush_interval
        self.max_pending = max_pending
        self._pending: dict[str, _Pending] = {}
        self._dropped = 0

    # --- Capture (synchronous, no I/O)------------------------------------------
    def capture(
        self,
        code: ErrorCode,
        exc: BaseException | None = None,
        *,
        message: str | None = None,
        **context: Any,
    ) -> str:
        exc_type = type(exc).__name__ if exc is not None else None
        location = _location(exc.__traceback__) if exc is not None else None
        fp = fingerprint(code, exc_type, location)
        text_message = (message or (str(exc) if exc is not None else code.title))[:_MAX_MESSAGE]
        ctx = _scrub({**structlog.contextvars.get_contextvars(), **context})

        getattr(log, _LOG_METHOD[code.severity])(
            text_message,
            error_code=code.code,
            error_title=code.title,
            fingerprint=fp,
            exc_info=exc if exc is not None and code.severity.rank >= Severity.ERROR.rank else None,
            **{k: v for k, v in ctx.items() if k not in {"request_id", "job_id"}},
        )

        if code.store and self.db is not None:
            pending = self._pending.get(fp)
            if pending is not None:
                pending.count += 1
                pending.message, pending.context = text_message, ctx
            elif len(self._pending) < self.max_pending:
                tb = (
                    "".join(traceback.format_exception(exc))[-_MAX_TRACEBACK:]
                    if exc is not None
                    else None
                )
                self._pending[fp] = _Pending(code, location, exc_type, text_message, tb, ctx)
            else:
                self._dropped += 1
        return fp

    # --- Flush------------------------------------------------------------------
    async def flush(self) -> int:
        if not self._pending or self.db is None:
            return 0
        batch, self._pending = self._pending, {}
        dropped, self._dropped = self._dropped, 0
        if dropped:
            log.warning("errors.dropped", count=dropped, error_code="TWR-INT-001")
        try:
            await self._write(batch)
        except Exception as exc:
            # No capture here (infinite loop): only log it.
            log.error("errors.flush_failed", error=str(exc), lost=len(batch))
            return 0
        return len(batch)

    async def _write(self, batch: dict[str, _Pending]) -> None:
        from tower.core.events.outbox import publish
        from tower.core.models.errors import ErrorReport

        async with self.db.transaction() as session:
            rows = await session.execute(
                select(ErrorReport.fingerprint, ErrorReport.resolved_at)
                .where(ErrorReport.fingerprint.in_(list(batch)))
                .with_for_update()
            )
            existing = {fp: resolved for fp, resolved in rows}
            for fp, p in batch.items():
                await session.execute(
                    _UPSERT,
                    {
                        "fp": fp,
                        "code": p.code.code,
                        "severity": p.code.severity.value,
                        "title": p.code.title,
                        "process": self.process,
                        "location": p.location,
                        "exc_type": p.exception_type,
                        "message": p.message,
                        "tb": p.traceback,
                        "ctx": orjson.dumps(p.context, default=str).decode(),
                        "n": p.count,
                    },
                )
                is_new = fp not in existing
                reopened = not is_new and existing[fp] is not None
                if (is_new or reopened) and p.code.severity.rank >= Severity.ERROR.rank:
                    await publish(
                        session,
                        "incident.reopened" if reopened else "incident.opened",
                        {
                            "fingerprint": fp,
                            "code": p.code.code,
                            "title": p.code.title,
                            "severity": p.code.severity.value,
                            "process": self.process,
                            "location": p.location,
                            "message": p.message,
                        },
                        aggregate_type="error_report",
                        aggregate_id=fp,
                        actor=f"system:{self.process}",
                    )

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self.flush_interval):
                    await stop.wait()
            await self.flush()


_UPSERT = text(
    """
    INSERT INTO error_reports (fingerprint, code, severity, title, process, location,
        exception_type, last_message, last_traceback, last_context, occurrences)
    VALUES (:fp, :code, :severity, :title, :process, :location, :exc_type, :message, :tb,
        CAST(:ctx AS jsonb), :n)
    ON CONFLICT (fingerprint) DO UPDATE SET
        occurrences = error_reports.occurrences + EXCLUDED.occurrences,
        last_seen = now(),
        process = EXCLUDED.process,
        last_message = EXCLUDED.last_message,
        last_traceback = COALESCE(EXCLUDED.last_traceback, error_reports.last_traceback),
        last_context = EXCLUDED.last_context,
        resolved_at = NULL,
        resolved_by = NULL
    """
)

# Process-wide reporter. By default it only logs (no database attached).
_current = ErrorReporter()


def set_reporter(reporter: ErrorReporter) -> None:
    global _current
    _current = reporter


def get_reporter() -> ErrorReporter:
    return _current


def capture(
    code: ErrorCode, exc: BaseException | None = None, *, message: str | None = None, **context: Any
) -> str:
    """Log the error and, if its code is storable, aggregate it for storage."""
    return _current.capture(code, exc, message=message, **context)
