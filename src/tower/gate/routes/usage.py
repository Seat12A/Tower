"""LLM usage report (free allocation of the day, monthly spend per scope)."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, date, datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel

from tower.core.domain.security import Permission
from tower.core.llm.usage import get_usage
from tower.gate.deps import ActorDep, ReadSession

router = APIRouter(prefix="/v1/llm", tags=["llm"])


class FreeUsageOut(BaseModel):
    provider: str
    free_group: str
    used_tokens: int
    allowance_tokens: int


class ScopeOut(BaseModel):
    scope: str
    spent_usd_micros: int
    reserved_usd_micros: int
    cap_usd_micros: int


class UsageOut(BaseModel):
    day: date
    usage_tier: int
    free: list[FreeUsageOut]
    period_start: date
    period_end: date
    scopes: list[ScopeOut]


@router.get("/usage", response_model=UsageOut)
async def llm_usage(request: Request, actor: ActorDep, session: ReadSession) -> UsageOut:
    actor.require(Permission.USAGE_READ)
    report = await get_usage(session, request.app.state.settings, datetime.now(UTC))
    return UsageOut(
        day=report.day,
        usage_tier=report.usage_tier,
        free=[FreeUsageOut(**asdict(f)) for f in report.free],
        period_start=report.period_start,
        period_end=report.period_end,
        scopes=[ScopeOut(**asdict(s)) for s in report.scopes],
    )
