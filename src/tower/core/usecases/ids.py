"""Resolve a short identifier (prefix of a public id) to the full public id."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain import error_codes as ec
from tower.core.domain.errors import Conflict, NotFound
from tower.core.domain.ids import prefix_bounds
from tower.core.domain.security import Actor, Permission
from tower.core.models.badges import BadgeAffiliation, BadgeOrgKey, BadgeReport, BadgeRequest
from tower.core.models.cases import Case
from tower.core.models.factcheck import Claim, ClaimGroup, Verdict
from tower.core.models.factcheck_v2 import Narrative

__all__ = ["KINDS", "resolve_id"]

KINDS: dict[str, Any] = {
    "case": Case,
    "group": ClaimGroup,
    "claim": Claim,
    "narrative": Narrative,
    "draft": Verdict,
    "affiliation": BadgeAffiliation,
    "badge_request": BadgeRequest,
    "badge_report": BadgeReport,
    "org_key": BadgeOrgKey,
}
_BADGE_KINDS = frozenset({"affiliation", "badge_request", "badge_report", "org_key"})


async def resolve_id(session: AsyncSession, actor: Actor, kind: str, ref: str) -> uuid.UUID:
    """Full public id of the ``kind`` item whose id starts with ``ref``. ``NotFound``
    when none does, ``Conflict`` (``TWR-GEN-005``) when several do."""
    actor.require(Permission.BADGES_READ if kind in _BADGE_KINDS else Permission.CASE_READ)
    model = KINDS[kind]
    low, high = prefix_bounds(ref)
    rows = (
        await session.scalars(
            select(model.public_id)
            .where(model.public_id >= low, model.public_id <= high)
            .order_by(model.public_id)
            .limit(2)
        )
    ).all()
    if not rows:
        raise NotFound("No item starts with this identifier", code=ec.GEN_NOT_FOUND, kind=kind)
    if len(rows) > 1:
        raise Conflict(
            "Several items start with this identifier", code=ec.GEN_AMBIGUOUS_ID, kind=kind
        )
    found: uuid.UUID = rows[0]
    return found
