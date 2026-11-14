"""E1 routing: post embeddings, "verifiable" score and discard rule, weekly partitions of
``content_embeddings``. See docs/factcheck/pipeline.md (E1).

Extraction of the N1 and sampled contents is lot 5: this stage only classifies and records.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import exists, not_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.config import Settings, get_settings
from tower.core.db.vector import EMBEDDING_DIMS
from tower.core.domain.factcheck.router import score as router_score
from tower.core.domain.security import Actor
from tower.core.events.outbox import EventRecord
from tower.core.factcheck import names, runtime
from tower.core.factcheck.profiles import active_profile
from tower.core.jobs.queue import enqueue
from tower.core.llm.errors import BudgetExhausted, to_job_error
from tower.core.logging import get_logger
from tower.core.models.factcheck_v1 import ContentEmbedding, RouterModel
from tower.core.models.ingestion import ContentItem, WatchedAccount
from tower.core.services.base import JobContext
from tower.core.usecases.routing import RoutingRow, record_routing

__all__ = ["embed_batch", "embedding_partitions", "on_content_to_embed"]

log = get_logger(__name__)

QUEUE = names.FC_BULK
MAX_TEXT_CHARS = 8000
_rng = random.Random()  # noqa: S311 (sampling, not security)
"""Sampling of the discarded contents (tests replace it with a seeded generator)."""
_model_cache: tuple[int, list[float], float, str, int] | None = None
"""``(version, weights, bias, embedding_model, dims)`` of the active router model: one
entry, reloaded when the active version changes."""


def _actor() -> Actor:
    return Actor.system(names.FACTCHECK_ACTOR)


# ------------------------------------------------------------------ subscription


async def on_content_to_embed(session: AsyncSession, events: Sequence[EventRecord]) -> None:
    """Debounced trigger of an embedding batch (one job per UTC minute). No network and
    no content read: copies are filtered by the job (they are linked after ingestion)."""
    languages = set(get_settings().fc_language_list)
    if not any(str(e.payload.get("lang", "")).lower() in languages for e in events):
        return
    await enqueue(
        session,
        QUEUE,
        names.JOB_EMBED_BATCH,
        {},
        dedupe_key=names.embed_batch_key(datetime.now(UTC).strftime("%Y%m%d%H%M")),
        delay=get_settings().fc_embed_debounce_seconds,
    )


# --------------------------------------------------------------------------- job


async def _active_router(session: AsyncSession) -> tuple[int, list[float], float, str, int] | None:
    global _model_cache
    version = await session.scalar(select(RouterModel.version).where(RouterModel.active))
    if version is None:
        _model_cache = None
        return None
    if _model_cache is None or _model_cache[0] != version:
        row = await session.scalar(select(RouterModel).where(RouterModel.version == version))
        if row is None:
            return None
        _model_cache = (row.version, list(row.weights), row.bias, row.embedding_model, row.dims)
    return _model_cache


async def embed_batch(ctx: JobContext) -> None:
    """Embed the pending contents, score them and record the routing decision.

    Idempotent: only contents without a ``content_embeddings`` row are selected.
    """
    settings = ctx.settings
    limit = settings.fc_embed_batch_size
    cutoff = datetime.now(UTC) - timedelta(days=settings.fc_embedding_retention_days)
    async with ctx.db.session() as session:
        watched = exists().where(
            WatchedAccount.account_id == ContentItem.author_account_id,
            WatchedAccount.active.is_(True),
        )
        rows = (
            await session.execute(
                select(ContentItem.id, ContentItem.text, ContentItem.lang, watched.label("w"))
                .where(
                    ContentItem.lang.in_(settings.fc_language_list),
                    ContentItem.copy_of_id.is_(None),
                    ContentItem.first_seen_at >= cutoff,
                    not_(exists().where(ContentEmbedding.content_id == ContentItem.id)),
                )
                .order_by(ContentItem.id)
                .limit(limit + 1)
            )
        ).all()
        profile = await active_profile(session, settings)
        router = await _active_router(session)
    if not rows:
        return
    more = len(rows) > limit
    rows = rows[:limit]
    threshold = profile.level("N1").e1_threshold
    embedder = runtime.get_embedder(settings, ctx.db)

    recorded = 0
    budget_error: BudgetExhausted | None = None
    for level, wanted in (("N3", True), ("N1", False)):
        group = [r for r in rows if bool(r.w) is wanted]
        if not group:
            continue
        try:
            result = await embedder.embed(
                [r.text[:MAX_TEXT_CHARS] for r in group], level=level, ref="embed_batch"
            )
        except BudgetExhausted as exc:
            budget_error = exc  # the other level may still fit its budget
            continue
        usable = (
            router is not None
            and router[3] == result.model
            and router[4] == EMBEDDING_DIMS == len(result.vectors[0])
        )
        out: list[RoutingRow] = []
        for item, vector in zip(group, result.vectors, strict=True):
            if usable and router is not None:
                value = router_score(router[1], router[2], vector)
                passed = value >= threshold
                sampled = (not passed) and _rng.random() < profile.e1_sample_rate
                out.append(
                    RoutingRow(
                        content_id=item.id,
                        embedding=vector,
                        model=result.model,
                        lang=item.lang,
                        score=value,
                        level="N1" if passed else "N0",
                        sampled=sampled,
                        router_version=router[0],
                    )
                )
            else:  # cold start: permissive rule, everything goes to E2
                out.append(
                    RoutingRow(
                        content_id=item.id,
                        embedding=vector,
                        model=result.model,
                        lang=item.lang,
                        score=None,
                        level="N1",
                    )
                )
        async with ctx.db.transaction() as session:
            recorded += await record_routing(session, _actor(), out)

    if budget_error is not None:
        raise to_job_error(budget_error) from budget_error
    log.info("factcheck.embed_batch", contents=len(rows), recorded=recorded, more=more)
    if more:
        async with ctx.db.transaction() as session:
            await enqueue(
                session,
                QUEUE,
                names.JOB_EMBED_BATCH,
                {},
                dedupe_key=names.embed_batch_key(f"next{ctx.job.id}"),
            )


# -------------------------------------------------------------------- partitions

_LIST = text(
    """
    SELECT c.relname FROM pg_inherits i
    JOIN pg_class c ON c.oid = i.inhrelid
    WHERE i.inhparent = 'content_embeddings'::regclass
    """
)
_EXISTS = text("SELECT to_regclass(:name) IS NOT NULL")


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def partition_name(week: date) -> str:
    return f"content_embeddings_p{week:%Y%m%d}"


async def embedding_partitions(session: AsyncSession, settings: Settings) -> int:
    """Create the partitions of the current week and the two next ones; drop those whose
    week ended more than ``fc_embedding_retention_days`` ago (no aggregation)."""
    now = datetime.now(UTC)
    start = _monday(now.date())
    changed = 0
    for i in range(3):
        week = start + timedelta(days=7 * i)
        name = partition_name(week)  # built from a date only: safe to inline
        if (await session.execute(_EXISTS, {"name": f"public.{name}"})).scalar_one():
            continue
        await session.execute(
            text(
                f'CREATE TABLE IF NOT EXISTS "{name}" PARTITION OF content_embeddings '
                f"FOR VALUES FROM ('{week.isoformat()} 00:00+00') "
                f"TO ('{(week + timedelta(days=7)).isoformat()} 00:00+00')"
            )
        )
        changed += 1
    cutoff = now - timedelta(days=settings.fc_embedding_retention_days)
    for (name,) in (await session.execute(_LIST)).all():
        if not (name.startswith("content_embeddings_p") and name[20:].isdigit()):
            continue
        week_end = datetime.strptime(name[20:], "%Y%m%d").replace(tzinfo=UTC) + timedelta(days=7)
        if week_end <= cutoff:
            # ``name`` is ``content_embeddings_p`` + digits: safe to inline as an identifier.
            await session.execute(text(f'DROP TABLE "{name}"'))
            changed += 1
    return changed
