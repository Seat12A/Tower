"""E4 scheduler (lot 5). STUB: implemented by subagent H3."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.config import Settings
from tower.core.services.base import JobContext


async def schedule(session: AsyncSession, settings: Settings) -> int:
    """Periodic singleton ``factcheck:schedule`` (ticks every minute; does a pass when the
    adaptive interval has elapsed)."""
    return 0


async def promotion_check(ctx: JobContext) -> None:
    """Job ``factcheck.promotion_check``: platform read confirming N2 metrics."""
    raise NotImplementedError
