"""Manual level overrides: read side (the most precise active override of a content)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import and_, case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain.factcheck.values import OverrideTarget
from tower.core.models.factcheck_v1 import LevelOverride

__all__ = ["override_level"]

_PRECISION = case(
    (LevelOverride.target_type == OverrideTarget.CONTENT.value, 0),
    (LevelOverride.target_type == OverrideTarget.GROUP.value, 1),
    else_=2,
)


async def override_level(
    session: AsyncSession,
    *,
    content_id: int | None,
    group_ids: Sequence[int],
    account_id: int | None,
    now: datetime | None = None,
) -> str | None:
    """Level of the most precise active override (content, then group, then account; the
    most recent first within a kind). Expired and revoked overrides are ignored."""
    targets = []
    if content_id is not None:
        targets.append(
            and_(
                LevelOverride.target_type == OverrideTarget.CONTENT.value,
                LevelOverride.target_id == content_id,
            )
        )
    if group_ids:
        targets.append(
            and_(
                LevelOverride.target_type == OverrideTarget.GROUP.value,
                LevelOverride.target_id.in_(list(group_ids)),
            )
        )
    if account_id is not None:
        targets.append(
            and_(
                LevelOverride.target_type == OverrideTarget.ACCOUNT.value,
                LevelOverride.target_id == account_id,
            )
        )
    if not targets:
        return None
    return await session.scalar(
        select(LevelOverride.level)
        .where(
            or_(*targets),
            LevelOverride.revoked_at.is_(None),
            LevelOverride.expires_at > (now or datetime.now(UTC)),
        )
        .order_by(_PRECISION, LevelOverride.created_at.desc(), LevelOverride.id.desc())
        .limit(1)
    )
