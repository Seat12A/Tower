"""E2 micro-batches for N1 contents (lot 5, mass flow).

``compose_batches`` (periodic) groups pending contents into dissimilar micro-batches and
enqueues ``factcheck.extract_batch``; the job extracts a batch with one model call, checks
fidelity post by post, and replays alone what cannot be trusted. Groups created here are
``dormant``: the scheduler decides what is investigated. See docs/factcheck/pipeline.md.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import exists, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.config import Settings
from tower.core.domain import error_codes as ec
from tower.core.domain.factcheck import batching as dom
from tower.core.domain.factcheck.fidelity import extract_numbers
from tower.core.domain.factcheck.values import BatchStatus, ExtractionStatus
from tower.core.domain.llm import Level
from tower.core.domain.security import Actor
from tower.core.domain.verification import Verification
from tower.core.errors import capture
from tower.core.factcheck import inputs, names
from tower.core.factcheck.profiles import active_profile
from tower.core.factcheck.schemas import ExtractBatchOutput, ExtractedClaim, ExtractOutput
from tower.core.jobs.queue import PermanentError, RetryLater, enqueue
from tower.core.llm.errors import AllRefused, BudgetExhausted
from tower.core.llm.prompts import PromptRegistry
from tower.core.logging import get_logger
from tower.core.models.factcheck import ContentClaim
from tower.core.models.factcheck_v1 import ContentEmbedding
from tower.core.models.factcheck_v2 import ContentExtraction, ExtractionAudit, ExtractionBatch
from tower.core.models.ingestion import ContentItem, WatchedAccount
from tower.core.ports.llm import LLMRequest
from tower.core.services.base import JobContext
from tower.core.usecases import claims as claims_uc
from tower.services.factcheck import matching
from tower.services.factcheck.jobs import (
    _EXTRACT_MAX_TOKENS,
    REPLAY_NOTE,
    _check_post_id,
    _complete,
    _faithful,
    _full_text,
    _retry_delay,
    _reuse_existing,
    _thread_text,
)

__all__ = ["compose_batches", "extract_batch"]

log = get_logger(__name__)

LEVEL = "N1"
PENDING_LIMIT = 500
_PER_POST_OUTPUT_TOKENS = 700
_MAX_OUTPUT_TOKENS = 8000
_rng = random.Random()  # noqa: S311 (sampling, not security)
"""Canary and audit draws (tests replace it with a seeded generator)."""

_IN_ACTIVE_JOB = text(
    "NOT EXISTS (SELECT 1 FROM jobs j WHERE j.kind = :kind AND j.status IN ('queued', 'running')"
    " AND j.payload->'content_ids' @> to_jsonb(content_items.id))"
).bindparams(kind=names.JOB_EXTRACT_BATCH)


def _actor() -> Actor:
    return Actor.system(names.FACTCHECK_ACTOR)


# ---------------------------------------------------------------------- composer


async def compose_batches(session: AsyncSession, settings: Settings) -> int:
    """Periodic ``factcheck:compose_batches``: enqueue ``factcheck.extract_batch`` jobs for
    the ready batches. Returns the number of jobs enqueued."""
    if not settings.fc_mass_flow:
        return 0
    from tower.services.factcheck import narratives

    watched = exists().where(
        WatchedAccount.account_id == ContentItem.author_account_id,
        WatchedAccount.active.is_(True),
    )
    rows = (
        await session.execute(
            select(
                ContentItem.id,
                ContentItem.text,
                ContentItem.first_seen_at,
                ContentEmbedding.embedding,
            )
            .join(ContentEmbedding, ContentEmbedding.content_id == ContentItem.id)
            .where(
                (ContentEmbedding.level == "N1") | ContentEmbedding.sampled.is_(True),
                ContentItem.lang.in_(settings.fc_language_list),
                ContentItem.copy_of_id.is_(None),
                ContentItem.verification != Verification.MISMATCH.value,
                ~watched,
                ~exists().where(ContentExtraction.content_id == ContentItem.id),
                ~exists().where(ContentClaim.content_id == ContentItem.id),
                _IN_ACTIVE_JOB,
            )
            .order_by(ContentItem.id)
            .limit(PENDING_LIMIT)
        )
    ).all()
    if not rows:
        return 0

    to_extract, counted = await narratives.filter_for_surges(
        session, settings, [r.id for r in rows]
    )
    if counted:
        await session.execute(
            pg_insert(ContentExtraction)
            .values(
                [
                    {
                        "content_id": cid,
                        "level": LEVEL,
                        "status": ExtractionStatus.SAMPLED_OUT.value,
                        "narrative_id": narrative_id,
                    }
                    for cid, narrative_id in counted.items()
                ]
            )
            .on_conflict_do_nothing()
        )
    keep = set(to_extract) - set(counted)
    selected = [r for r in rows if r.id in keep]
    candidates = [
        dom.BatchCandidate(
            content_id=r.id,
            tokens=dom.estimate_tokens(r.text),
            vector=r.embedding,
            narrative_id=None,
            solo=dom.has_injection_marker(r.text),
        )
        for r in selected
    ]
    by_id = {r.id: r for r in selected}
    solo_ids = {
        c.content_id for c in candidates if c.solo or c.tokens > settings.fc_batch_max_tokens
    }
    batches = dom.compose(
        candidates,
        size=settings.fc_batch_size,
        max_tokens=settings.fc_batch_max_tokens,
        dissimilarity=settings.fc_batch_dissimilarity,
    )
    prompt_version = PromptRegistry.default().get(names.prompt_id(names.ROLE_EXTRACT_BATCH)).version
    now = datetime.now(UTC)
    enqueued = 0
    for batch in batches:
        oldest = min(by_id[i].first_seen_at for i in batch)
        isolated = len(batch) == 1 and batch[0] in solo_ids
        if not isolated and not dom.ready(
            batch,
            oldest_age_s=(now - oldest).total_seconds(),
            size=settings.fc_batch_size,
            max_wait_s=settings.fc_batch_max_wait_s,
        ):
            continue
        job_id = await enqueue(
            session,
            names.FC_BULK,
            names.JOB_EXTRACT_BATCH,
            {"content_ids": batch},
            dedupe_key=names.extract_batch_key(batch, prompt_version),
        )
        enqueued += job_id is not None
    if enqueued or counted:
        log.info(
            "factcheck.batches_composed",
            pending=len(rows),
            sampled_out=len(counted),
            batches=enqueued,
        )
    return enqueued


# --------------------------------------------------------------------------- job


@dataclass(slots=True)
class _Post:
    content_id: int
    position: int
    lang: str
    text: str
    created: datetime | None
    context_text: str
    model_input: dict[str, Any]
    """Full ``extract_input`` dict (solo replays); the batch uses its ``post`` entry."""


@dataclass(slots=True)
class _Extracted:
    claims: list[ExtractedClaim]
    output: ExtractOutput
    prompt_version: str


class _Costs:
    def __init__(self) -> None:
        self.micros = 0


async def _extracted_already(session: AsyncSession, content_id: int) -> bool:
    return bool(
        await session.scalar(
            select(
                exists().where(ContentExtraction.content_id == content_id)
                | exists().where(ContentClaim.content_id == content_id)
            )
        )
    )


async def _load_posts(
    session: AsyncSession, settings: Settings, ids: list[int], today: date
) -> list[_Post]:
    """Contents still to extract (batch positions follow the payload order)."""
    contents = {
        c.id: c
        for c in (await session.scalars(select(ContentItem).where(ContentItem.id.in_(ids)))).all()
    }
    posts: list[_Post] = []
    for position, content_id in enumerate(ids):
        content = contents.get(content_id)
        if content is None or content.lang not in settings.fc_language_list:
            continue
        if await _reuse_existing(session, _actor(), content, LEVEL, names.FACTCHECK_ACTOR, False):
            await claims_uc.mark_extraction(
                session, content_id, LEVEL, ExtractionStatus.EXTRACTED, None, None
            )
            continue
        if await _extracted_already(session, content_id):
            continue
        kind, context_text = await _thread_text(session, content)
        posts.append(
            _Post(
                content_id=content_id,
                position=position,
                lang=content.lang,
                text=content.text,
                created=content.created_at_platform,
                context_text=context_text,
                model_input=inputs.extract_input(
                    post_id=str(content_id),
                    lang=content.lang,
                    text=content.text,
                    created_at=content.created_at_platform,
                    today=today,
                    context_kind=kind,
                    context_text=context_text or None,
                ),
            )
        )
    return posts


async def _solo_call(
    ctx: JobContext, post: _Post, costs: _Costs, *, replay: bool = False
) -> tuple[ExtractOutput, str] | None:
    """One post extracted alone (role ``extract``, level N1). ``None``: model refused."""
    payload = {**post.model_input, "replay": REPLAY_NOTE} if replay else post.model_input
    request = LLMRequest(
        role=names.ROLE_EXTRACT,
        level=Level(LEVEL),
        prompt_id=names.prompt_id(names.ROLE_EXTRACT),
        input=payload,
        schema=ExtractOutput,
        max_output_tokens=_EXTRACT_MAX_TOKENS,
        budget_scope=names.level_scope(LEVEL),
    )
    try:
        result = await _complete(ctx, request)
    except AllRefused as exc:
        capture(ec.FCK_MODEL_REFUSED, exc, stage="extract_batch_solo")
        return None
    costs.micros += result.cost_usd_micros
    if not isinstance(result.output, ExtractOutput):
        raise TypeError("unexpected extraction output type")
    return result.output, result.prompt_version


async def _solo_extract(ctx: JobContext, post: _Post, costs: _Costs) -> _Extracted | None:
    """Solo extraction with the usual fidelity check and one replay."""
    first = await _solo_call(ctx, post, costs)
    if first is None:
        return None
    output, version = first
    _check_post_id(output, post.content_id)
    claims, failed = _faithful(output, post.text, post.context_text, post.content_id)
    if failed:
        replay = await _solo_call(ctx, post, costs, replay=True)
        if replay is None:
            return None
        output, version = replay
        _check_post_id(output, post.content_id)
        claims, _ = _faithful(output, post.text, post.context_text, post.content_id)
    return _Extracted(claims, output, version)


async def _batch_call(
    ctx: JobContext,
    batch_id: int,
    posts: list[_Post],
    canary: tuple[str, str] | None,
    today: date,
    costs: _Costs,
) -> tuple[ExtractBatchOutput, str] | None:
    entries = [p.model_input["post"] for p in posts]
    if canary is not None:
        entry = inputs.extract_input(
            post_id=f"{dom.CANARY_PREFIX}{batch_id}",
            lang="fr",
            text=canary[0],
            created_at=None,
            today=today,
        )["post"]
        entries.insert(_rng.randrange(len(entries) + 1), entry)
    request = LLMRequest(
        role=names.ROLE_EXTRACT_BATCH,
        level=Level(LEVEL),
        prompt_id=names.prompt_id(names.ROLE_EXTRACT_BATCH),
        input=inputs.extract_batch_input(posts=entries, today=today),
        schema=ExtractBatchOutput,
        max_output_tokens=min(_MAX_OUTPUT_TOKENS, 400 + _PER_POST_OUTPUT_TOKENS * len(entries)),
        budget_scope=names.level_scope(LEVEL),
    )
    try:
        result = await _complete(ctx, request)
    except AllRefused as exc:
        capture(ec.FCK_MODEL_REFUSED, exc, stage="extract_batch")
        return None
    costs.micros += result.cost_usd_micros
    if not isinstance(result.output, ExtractBatchOutput):
        raise TypeError("unexpected batch extraction output type")
    return result.output, result.prompt_version


@dataclass(slots=True)
class _Outcome:
    status: BatchStatus
    failures: int
    contamination: bool
    prompt_version: str | None
    results: dict[int, _Extracted]
    """Batch results kept (posts not in it were replayed alone or refused)."""
    solo: dict[int, _Extracted | None]


async def _extract_posts(
    ctx: JobContext,
    batch_id: int,
    posts: list[_Post],
    canary: tuple[str, str] | None,
    today: date,
    costs: _Costs,
) -> _Outcome:
    """Call the model for the batch, judge it, replay alone what must be."""
    called = await _batch_call(ctx, batch_id, posts, canary, today, costs)
    kept: dict[int, _Extracted] = {}
    to_replay: list[_Post] = list(posts)
    failures = 0
    contamination = False
    version: str | None = None
    if called is not None:
        output, version = called
        by_id: dict[str, ExtractOutput] = {}
        for item in output.posts:
            by_id.setdefault(item.post_id, item)
        known = {str(p.content_id) for p in posts}
        missing = [p for p in posts if str(p.content_id) not in by_id]
        if missing:
            capture(
                ec.FCK_BATCH_INVALID,
                batch=batch_id,
                missing=len(missing),
                returned=len(by_id),
            )
        ignored = [i for i in by_id if i not in known and not i.startswith(dom.CANARY_PREFIX)]
        if ignored:
            log.info("factcheck.batch_unknown_ids", batch=batch_id, count=len(ignored))
        if canary is not None:
            contamination = dom.canary_contaminated(
                canary[1],
                {pid: [_full_text(c) for c in item.claims] for pid, item in by_id.items()},
            )
            if contamination:
                capture(ec.FCK_BATCH_CONTAMINATION, batch=batch_id, size=len(posts))
        judged: dict[int, _Extracted] = {}
        for post in posts:
            returned = by_id.get(str(post.content_id))
            if returned is None:
                continue
            claims, failed = _faithful(returned, post.text, post.context_text, post.content_id)
            if failed:
                failures += 1
                continue
            judged[post.content_id] = _Extracted(claims, returned, version)
        if not contamination and failures < 2:
            kept = judged
            to_replay = [p for p in posts if p.content_id not in kept]
    solo: dict[int, _Extracted | None] = {}
    for post in to_replay:
        solo[post.content_id] = await _solo_extract(ctx, post, costs)
    replayed_all = not kept
    status = BatchStatus.REPLAYED_SOLO if contamination or failures >= 2 else BatchStatus.DONE
    if called is None and replayed_all:
        status = BatchStatus.REPLAYED_SOLO
    return _Outcome(status, failures, contamination, version, kept, solo)


async def _record_post(
    ctx: JobContext, batch_id: int, post: _Post, extracted: _Extracted | None
) -> None:
    """E3 matching then ``record_extraction`` (dormant groups), one transaction."""
    settings = ctx.settings
    actor = _actor()
    if extracted is None:  # the model refused the post: never retried
        async with ctx.db.transaction() as session:
            await claims_uc.mark_extraction(
                session, post.content_id, LEVEL, ExtractionStatus.FAILED, batch_id, post.position
            )
        return
    created = post.created
    post_date = created.date() if created is not None else None
    decisions = None
    if extracted.claims:
        try:
            decisions = await matching.match_claims(
                ctx,
                content_id=post.content_id,
                post_date=post_date,
                lang=post.lang,
                claims=extracted.claims,
                level=LEVEL,
            )
        except BudgetExhausted as exc:
            raise RetryLater(_retry_delay(exc.retry_at), "model budget exhausted") from exc
    async with ctx.db.transaction() as session:
        content = await session.get(ContentItem, post.content_id)
        if content is None:
            return
        if await _reuse_existing(session, actor, content, LEVEL, names.FACTCHECK_ACTOR, False):
            await claims_uc.mark_extraction(
                session, post.content_id, LEVEL, ExtractionStatus.EXTRACTED, None, None
            )
            return
        if await session.scalar(
            select(exists().where(ContentExtraction.content_id == post.content_id))
        ):
            return
        profile = await active_profile(session, settings)
        record = await claims_uc.record_extraction(
            session,
            actor,
            content=content,
            claims=extracted.claims,
            level=LEVEL,
            requested_by=names.FACTCHECK_ACTOR,
            prompt_version=extracted.prompt_version,
            thread_context=extracted.output.thread_context,
            absence_reason=(
                extracted.output.absence_reason.value if extracted.output.absence_reason else None
            ),
            matches=decisions,
            post_date=post_date,
            profile=profile,
            current_period_days=settings.fc_current_period_days,
            investigate=False,
            batch_id=batch_id,
            position=post.position,
        )
    log.info(
        "factcheck.batch_post_extracted",
        content=post.content_id,
        batch=batch_id,
        claims=len(record.claim_ids),
    )


def _figures(claims: list[ExtractedClaim]) -> list[str]:
    return sorted(set().union(*(extract_numbers(_full_text(c)) for c in claims)) if claims else [])


async def _audit(
    ctx: JobContext, batch_id: int, posts: list[_Post], outcome: _Outcome, costs: _Costs
) -> None:
    """Replay a few batched posts alone (omission measurement). The batch result is kept:
    the solo replay is not ground truth. Best effort."""
    positions = set(dom.audit_positions(len(posts), ctx.settings.fc_solo_audit_rate, _rng))
    rows: list[ExtractionAudit] = []
    for index, post in enumerate(posts):
        kept = outcome.results.get(post.content_id)
        if index not in positions or kept is None:
            continue
        replay = await _solo_call(ctx, post, costs)
        if replay is None:
            continue
        solo_output, _ = replay
        batch_figures, solo_figures = _figures(kept.claims), _figures(solo_output.claims)
        discordant = len(kept.claims) != len(solo_output.claims) or batch_figures != solo_figures
        rows.append(
            ExtractionAudit(
                content_id=post.content_id,
                batch_id=batch_id,
                position=post.position,
                batch_size=len(posts),
                batch_claims=len(kept.claims),
                solo_claims=len(solo_output.claims),
                discordant=discordant,
                details={"batch_figures": batch_figures, "solo_figures": solo_figures},
            )
        )
    if rows:
        async with ctx.db.transaction() as session:
            session.add_all(rows)


async def _finish(
    ctx: JobContext,
    batch_id: int,
    status: BatchStatus,
    *,
    failures: int = 0,
    contamination: bool = False,
    prompt_version: str | None = None,
    cost: int = 0,
) -> None:
    async with ctx.db.transaction() as session:
        await session.execute(
            text(
                "UPDATE extraction_batches SET status = :s, fidelity_failures = :f,"
                " contamination = :c, prompt_version = :p, cost_usd_micros = :u,"
                " finished_at = now() WHERE id = :i"
            ),
            {
                "s": status.value,
                "f": failures,
                "c": contamination,
                "p": prompt_version,
                "u": cost,
                "i": batch_id,
            },
        )


async def extract_batch(ctx: JobContext) -> None:
    """Job ``factcheck.extract_batch``: one model call for up to 12 dissimilar posts.

    Idempotent: contents already extracted are skipped, a finished batch does nothing.
    """
    try:
        ids = [int(i) for i in ctx.job.payload["content_ids"]]
    except (KeyError, TypeError, ValueError) as exc:
        raise PermanentError("bad extract_batch payload") from exc
    if not ids or len(ids) > dom.BATCH_HARD_CAP:
        raise PermanentError("bad extract_batch size")
    settings = ctx.settings
    today = datetime.now(UTC).date()

    async with ctx.db.transaction() as session:
        posts = await _load_posts(session, settings, ids, today)
        if not posts:
            log.info("factcheck.batch_nothing_to_do", contents=len(ids))
            return
        canary: tuple[str, str] | None = None
        if _rng.random() < settings.fc_canary_rate:
            canary = dom.canary_post(_rng.getrandbits(32))
        batch = ExtractionBatch(
            content_ids=[p.content_id for p in posts],
            canary_token=canary[1] if canary else None,
            status=BatchStatus.PENDING.value,
        )
        session.add(batch)
        await session.flush()
        batch_id = batch.id

    costs = _Costs()
    try:
        outcome = await _extract_posts(ctx, batch_id, posts, canary, today, costs)
        for post in posts:
            extracted = outcome.results.get(post.content_id)
            if extracted is None:
                extracted = outcome.solo.get(post.content_id)
            await _record_post(ctx, batch_id, post, extracted)
        try:
            await _audit(ctx, batch_id, posts, outcome, costs)
        except (RetryLater, PermanentError) as exc:
            log.info("factcheck.batch_audit_skipped", batch=batch_id, reason=str(exc))
    except Exception:
        await _finish(ctx, batch_id, BatchStatus.FAILED, cost=costs.micros)
        raise
    await _finish(
        ctx,
        batch_id,
        outcome.status,
        failures=outcome.failures,
        contamination=outcome.contamination,
        prompt_version=outcome.prompt_version,
        cost=costs.micros,
    )
    log.info(
        "factcheck.batch_extracted",
        batch=batch_id,
        posts=len(posts),
        status=outcome.status.value,
        failures=outcome.failures,
        contamination=outcome.contamination,
        cost_usd_micros=costs.micros,
    )
