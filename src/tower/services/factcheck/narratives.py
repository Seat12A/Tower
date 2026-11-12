"""Narratives and waves (lot 5). STUB: implemented by subagent H4."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.config import Settings
from tower.core.events.outbox import EventRecord
from tower.core.services.base import JobContext


async def on_group_created(session: AsyncSession, events: Sequence[EventRecord]) -> None:
    """Subscription on ``claim_group.created``: enqueue ``factcheck.attach_narrative``."""


async def attach_narrative(ctx: JobContext) -> None:
    """Job ``factcheck.attach_narrative``."""
    raise NotImplementedError


async def narrative_title(ctx: JobContext) -> None:
    """Job ``factcheck.narrative_title``."""
    raise NotImplementedError


async def waves_step(session: AsyncSession, settings: Settings, now: datetime) -> dict[str, Any]:
    """Called by the scheduler pass: window stats, tiers, wave mode effects."""
    return {}


async def maintain_narratives(session: AsyncSession, settings: Settings) -> int:
    """Periodic ``factcheck:narratives``: closure after 7 days, medoid re-election."""
    return 0
