"""Erreurs stockées : consultation et résolution (admins, bot)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from tower.core.domain.error_codes import CATALOG
from tower.core.errors import service
from tower.core.models.errors import ErrorReport
from tower.gate.deps import ActorDep, ReadSession, WriteSession

router = APIRouter(prefix="/v1/errors", tags=["erreurs"])


class ErrorReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    fingerprint: str
    code: str
    severity: str
    title: str
    process: str
    location: str | None
    exception_type: str | None
    last_message: str | None
    last_context: dict[str, Any]
    occurrences: int
    first_seen: datetime
    last_seen: datetime
    resolved_at: datetime | None
    resolved_by: str | None
    resolution_note: str | None


class ErrorReportDetail(ErrorReportOut):
    last_traceback: str | None


class ResolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str | None = Field(default=None, max_length=1000)


class ErrorCodeOut(BaseModel):
    code: str
    title: str
    http_status: int
    severity: str
    store: bool
    retryable: bool
    hint: str


@router.get("/catalog", response_model=list[ErrorCodeOut])
async def catalog() -> list[ErrorCodeOut]:
    """Catalogue public des codes (utile aux clients : site, extension, bot)."""
    return [
        ErrorCodeOut(
            code=c.code,
            title=c.title,
            http_status=c.http_status,
            severity=c.severity.value,
            store=c.store,
            retryable=c.retryable,
            hint=c.hint,
        )
        for c in CATALOG.values()
        if not c.deprecated
    ]


@router.get("", response_model=list[ErrorReportOut])
async def list_errors(
    actor: ActorDep,
    session: ReadSession,
    include_resolved: bool = False,
    code: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ErrorReport]:
    rows = await service.list_reports(
        session, actor, include_resolved=include_resolved, code=code, limit=limit
    )
    return list(rows)


@router.get("/{fingerprint}", response_model=ErrorReportDetail)
async def get_error(fingerprint: str, actor: ActorDep, session: ReadSession) -> ErrorReport:
    return await service.get_report(session, actor, fingerprint)


@router.post("/{fingerprint}/resolve", response_model=ErrorReportOut)
async def resolve_error(
    fingerprint: str, body: ResolveIn, actor: ActorDep, session: WriteSession
) -> ErrorReport:
    return await service.resolve(session, actor, fingerprint, note=body.note)
