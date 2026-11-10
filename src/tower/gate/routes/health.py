from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from tower import __version__
from tower.core.domain import error_codes as ec
from tower.core.errors import capture
from tower.gate.deps import DbDep

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness: does not touch the database (Railway healthcheck)."""
    return {"status": "ok", "version": __version__}


@router.get("/ready", response_model=None)
async def ready(db: DbDep) -> JSONResponse | dict[str, str]:
    """Readiness: the database responds."""
    try:
        await db.ping()
    except Exception as exc:
        capture(ec.DB_UNAVAILABLE, exc)
        return JSONResponse(
            {"status": "unavailable", "code": ec.DB_UNAVAILABLE.code}, status_code=503
        )
    return {"status": "ready"}
