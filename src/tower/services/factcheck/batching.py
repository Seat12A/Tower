"""E2 micro-batches for N1 contents (lot 5). STUB: implemented by subagent H2."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.config import Settings
from tower.core.services.base import JobContext


async def compose_batches(session: AsyncSession, settings: Settings) -> int:
    """Periodic ``factcheck:compose_batches``: enqueue ``factcheck.extract_batch`` jobs."""
    return 0


async def extract_batch(ctx: JobContext) -> None:
    """Job ``factcheck.extract_batch``."""
    raise NotImplementedError
