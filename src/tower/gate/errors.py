"""Single error format for the API ::

    {"error": {"code": "TWR-DOS-002", "message": "...", "details": {...},
               "request_id": "...", "fingerprint": "..."}}

- Domain errors (``DomainError``): status and code taken from the catalog.
- FastAPI validation: ``TWR-GEN-001`` (422), details per field.
- Unexpected exceptions: ``TWR-INT-001`` (500), stored, without leaking information;
  ``fingerprint`` and ``request_id`` make it possible to find the trace.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from tower.core.domain import error_codes as ec
from tower.core.domain.errors import DomainError
from tower.core.errors import capture

_HTTP_CODES = {
    404: ec.GEN_NOT_FOUND,
    405: ec.GEN_INVALID_INPUT,
    429: ec.GEN_RATE_LIMITED,
}


def error_body(
    code: ec.ErrorCode,
    message: str,
    details: dict[str, Any] | None = None,
    fingerprint: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code.code, "message": message, "details": details or {}}
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    if request_id:
        body["request_id"] = request_id
    if fingerprint:
        body["fingerprint"] = fingerprint
    return {"error": body}


def unexpected_error_response(exc: BaseException) -> JSONResponse:
    fp = capture(ec.INT_UNEXPECTED, exc)
    return JSONResponse(
        error_body(ec.INT_UNEXPECTED, "Internal error", fingerprint=fp), status_code=500
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain(_: Request, exc: DomainError) -> JSONResponse:
        fp = capture(exc.error_code, exc, message=exc.message, **_flat(exc.details))
        return JSONResponse(
            error_body(
                exc.error_code,
                exc.message,
                jsonable_encoder(exc.details),
                fp if exc.error_code.store else None,
            ),
            status_code=exc.http_status,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"field": ".".join(str(p) for p in err.get("loc", ())), "message": err.get("msg")}
            for err in exc.errors()
        ]
        return JSONResponse(
            error_body(ec.GEN_INVALID_INPUT, "Invalid request", {"errors": errors}),
            status_code=422,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, ec.GEN_INVALID_INPUT)
        return JSONResponse(
            error_body(code, str(exc.detail)), status_code=exc.status_code, headers=exc.headers
        )


def _flat(details: dict[str, object]) -> dict[str, object]:
    return {f"detail_{k}": v for k, v in details.items()}
