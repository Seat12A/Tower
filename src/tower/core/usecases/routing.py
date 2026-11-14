"""E1 routing: recording the routing decisions of contents and managing the router
models (logistic regression weights). See docs/factcheck/pipeline.md (E1).

``record_routing`` is high volume: no event (a topic per content would flood the
outbox). Activating a router model is audited by ``router_models.activated_by/at`` and
a log line (no topic: the model is internal to the E1 stage).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.db.vector import EMBEDDING_DIMS
from tower.core.domain import error_codes as ec
from tower.core.domain.errors import InvalidInput, NotFound
from tower.core.domain.factcheck.router import MIN_RECALL_TO_ACTIVATE
from tower.core.domain.security import Actor, Permission
from tower.core.logging import get_logger
from tower.core.models.factcheck_v1 import ContentEmbedding, RouterModel

__all__ = ["RoutingRow", "activate_router_model", "create_router_model", "record_routing"]

log = get_logger(__name__)

_LOCK_KEY = 0x45315F52  # advisory lock: one recorder at a time (no duplicate rows)
_LEVELS = ("N0", "N1")


@dataclass(frozen=True, slots=True)
class RoutingRow:
    content_id: int
    embedding: Sequence[float]
    model: str
    lang: str
    score: float | None
    level: str
    sampled: bool = False
    router_version: int | None = None


async def record_routing(session: AsyncSession, actor: Actor, rows: Sequence[RoutingRow]) -> int:
    """Insert the routing rows of contents that have none yet. Returns how many."""
    actor.require(Permission.FACTCHECK_REQUEST)
    if not rows:
        return 0
    for row in rows:
        if row.level not in _LEVELS:
            raise InvalidInput(
                "Invalid routing level", code=ec.FCK_ROUTER_MODEL_INVALID, level=row.level
            )
        if len(row.embedding) != EMBEDDING_DIMS:
            raise InvalidInput(
                "Invalid embedding size",
                code=ec.FCK_ROUTER_MODEL_INVALID,
                dims=len(row.embedding),
            )
    # The primary key includes created_at, so uniqueness per content is enforced here.
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
    existing = set(
        await session.scalars(
            select(ContentEmbedding.content_id).where(
                ContentEmbedding.content_id.in_([r.content_id for r in rows])
            )
        )
    )
    fresh = {r.content_id: r for r in rows if r.content_id not in existing}
    if not fresh:
        return 0
    await session.execute(
        pg_insert(ContentEmbedding).on_conflict_do_nothing(),
        [
            {
                "content_id": r.content_id,
                "embedding": list(r.embedding),
                "model": r.model,
                "lang": r.lang,
                "score": r.score,
                "level": r.level,
                "sampled": r.sampled,
                "router_version": r.router_version,
            }
            for r in fresh.values()
        ],
    )
    return len(fresh)


async def create_router_model(
    session: AsyncSession,
    actor: Actor,
    *,
    embedding_model: str,
    weights: Sequence[float],
    bias: float,
    metrics: Mapping[str, Any],
    sample_size: int,
) -> RouterModel:
    """Store a new INACTIVE router model (next version)."""
    actor.require(Permission.PROFILES_MANAGE)
    if not weights or not all(math.isfinite(w) for w in weights) or not math.isfinite(bias):
        raise InvalidInput(
            "Router weights must be finite numbers", code=ec.FCK_ROUTER_MODEL_INVALID
        )
    if len(weights) != EMBEDDING_DIMS:
        raise InvalidInput(
            "Router weights do not match the embedding size",
            code=ec.FCK_ROUTER_MODEL_INVALID,
            dims=len(weights),
            expected=EMBEDDING_DIMS,
        )
    latest = await session.scalar(select(func.max(RouterModel.version)))
    version = (latest or 0) + 1
    row = RouterModel(
        version=version,
        embedding_model=embedding_model,
        dims=len(weights),
        weights=[float(w) for w in weights],
        bias=float(bias),
        metrics=dict(metrics),
        sample_size=sample_size,
        active=False,
    )
    session.add(row)
    await session.flush()
    log.info("factcheck.router_created", version=version, sample_size=sample_size)
    return row


async def activate_router_model(
    session: AsyncSession,
    actor: Actor,
    *,
    version: int,
    expected_model: str,
    expected_dims: int = EMBEDDING_DIMS,
    force: bool = False,
    reason: str,
) -> RouterModel:
    """Activate ``version`` and deactivate the previous one (same transaction).

    Refused (``TWR-FCK-036``) when its model or size does not match the embeddings in
    use, or when its held-out recall is below ``MIN_RECALL_TO_ACTIVATE`` (``force``
    overrides the recall rule only).
    """
    actor.require(Permission.PROFILES_MANAGE)
    row = await session.scalar(
        select(RouterModel).where(RouterModel.version == version).with_for_update()
    )
    if row is None:
        raise NotFound("Router model not found", code=ec.FCK_ROUTER_MODEL_INVALID, version=version)
    if row.embedding_model != expected_model or row.dims != expected_dims:
        raise InvalidInput(
            "Router model does not match the embedding model in use",
            code=ec.FCK_ROUTER_MODEL_INVALID,
            version=version,
            model=row.embedding_model,
            expected=expected_model,
        )
    recall = row.metrics.get("recall")
    if not force and (not isinstance(recall, int | float) or recall < MIN_RECALL_TO_ACTIVATE):
        raise InvalidInput(
            "Held-out recall is below the activation target (use --force to override)",
            code=ec.FCK_ROUTER_MODEL_INVALID,
            version=version,
            recall=recall,
            target=MIN_RECALL_TO_ACTIVATE,
        )
    now = datetime.now(UTC)
    await session.execute(
        update(RouterModel).where(RouterModel.active.is_(True)).values(active=False)
    )
    await session.execute(
        update(RouterModel)
        .where(RouterModel.id == row.id)
        .values(active=True, activated_at=now, activated_by=actor.label)
    )
    await session.refresh(row)
    log.info("factcheck.router_activated", version=version, forced=force, reason=reason[:200])
    return row
