"""Job handlers of the deep investigation: one job per stage.

Every handler follows the same protocol: load the investigation; if its stage (or round)
no longer matches the job, return (the job was already applied); run the model or Atlas
step OUTSIDE any transaction (the router caches successes, a replay pays nothing); then
ONE transaction that locks the investigation, stores the output, adds the cost, advances
the stage and enqueues the next job with its stage key. The step logic itself lives in
``pipeline.py`` (shared with the evaluation tool).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.db.database import Database
from tower.core.domain import error_codes as ec
from tower.core.domain.factcheck.opinion import compare_opinions
from tower.core.domain.factcheck.values import (
    UNVERIFIABLE_CLAIM_TYPES,
    Agreement,
    InvestigationStage,
)
from tower.core.domain.security import Actor
from tower.core.errors import capture
from tower.core.events.outbox import EventRecord
from tower.core.factcheck import names, runtime
from tower.core.factcheck.inputs import ClaimView
from tower.core.factcheck.schemas import (
    FrameOutput,
    JudgeOutput,
    ObjectionOutput,
    SecondOpinionOutput,
)
from tower.core.jobs.queue import PermanentError, RetryLater, enqueue
from tower.core.llm.errors import (
    AllRefused,
    BudgetExhausted,
    ProviderNotConfigured,
    ProviderUnavailable,
    to_job_error,
)
from tower.core.logging import get_logger
from tower.core.models.factcheck import (
    Claim,
    ClaimGroup,
    EvidencePassage,
    Investigation,
    Source,
)
from tower.core.models.ingestion import ContentItem
from tower.core.services.base import JobContext
from tower.core.usecases import investigations as uc
from tower.services.investigate import pipeline as pl

log = get_logger(__name__)

ACTOR = Actor.system(names.INVESTIGATE_ACTOR)
_STAGE_OF_JOB = {
    names.JOB_FRAME: "frame",
    names.JOB_COLLECT: "collect",
    names.JOB_JUDGE: "judge",
    names.JOB_SECOND_OPINION: "second_opinion",
    names.JOB_OBJECTION: "objection",
    names.JOB_ESCALATE: "escalate",
    names.JOB_DRAFT: "draft",
}
_INTERNAL_FLAGS = ("versions", "synthetic_judgement")
_MIN_RETRY_S = 60.0
_MAX_RETRY_S = 6 * 3600.0
_STAGES = InvestigationStage


# ------------------------------------------------------------------------- loading


@dataclass(slots=True)
class _State:
    inv: Investigation
    claim: Claim
    group: ClaimGroup
    content: ContentItem | None
    view: ClaimView
    post_dt: datetime | None
    today: date


def _num(value: Any) -> float | None:
    return None if value is None else float(value)


def _iso(value: Any) -> str | None:
    return None if value is None else value.isoformat()


def claim_view(claim: Claim) -> ClaimView:
    """What the models know about a claim: the claims row, never the post or its author."""
    return ClaimView(
        text_original=claim.text_original,
        lang=claim.lang_original,
        text_fr=claim.text_fr,
        claim_type=claim.claim_type,
        jurisdictions=tuple(claim.jurisdictions or ()),
        primary_jurisdiction=claim.primary_jurisdiction,
        signature={
            "subject": claim.subject,
            "subject_qid": claim.subject_qid,
            "predicate": claim.predicate,
            "value_low": _num(claim.value_low),
            "value_high": _num(claim.value_high),
            "unit": claim.unit,
            "unit_scale": claim.unit_scale,
            "currency": claim.currency,
            "value_date": _iso(claim.value_date),
            "period_start": _iso(claim.period_start),
            "period_end": _iso(claim.period_end),
            "place": claim.place,
            "polarity": claim.polarity,
            "invoked_source": claim.invoked_source,
            "definition_variant": claim.definition_variant,
            "comparison_base": claim.comparison_base,
        },
        entities=tuple(
            {"name": e.get("name", ""), "role": e.get("role", "other")}
            for e in (claim.entities or ())
        ),
        context_hints=tuple(claim.context_hints or ()),
        thread_context=claim.thread_context,
    )


async def _load(ctx: JobContext, expected: _STAGES) -> _State | None:
    """The investigation of the job, or ``None`` when the job no longer applies."""
    try:
        inv_id = int(ctx.job.payload["investigation_id"])
    except (KeyError, TypeError, ValueError):
        log.warning("investigate.bad_payload", job=ctx.job.kind)
        return None
    async with ctx.db.session() as session:
        inv = await session.get(Investigation, inv_id)
        if inv is None:
            log.info("investigate.investigation_missing", investigation=inv_id)
            return None
        if inv.stage != expected.value or int(ctx.job.payload.get("round", inv.round)) != inv.round:
            log.info(
                "investigate.job_skipped",
                job=ctx.job.kind,
                investigation=inv_id,
                stage=inv.stage,
                round=inv.round,
            )
            return None
        group = await session.get(ClaimGroup, inv.claim_group_id)
        claim = await session.get(Claim, group.representative_claim_id) if group else None
        if group is None or claim is None:
            log.warning("investigate.claim_missing", investigation=inv_id)
            return None
        content = await session.get(ContentItem, inv.content_id) if inv.content_id else None
    post_dt = None
    if content is not None:
        post_dt = content.created_at_platform or content.first_seen_at
        if post_dt is not None and post_dt.tzinfo is None:
            post_dt = post_dt.replace(tzinfo=UTC)
    return _State(
        inv=inv,
        claim=claim,
        group=group,
        content=content,
        view=claim_view(claim),
        post_dt=post_dt,
        today=datetime.now(UTC).date(),
    )


async def _load_passages(db: Database, investigation_id: int) -> list[pl.PassageData]:
    async with db.session() as session:
        rows = (
            await session.execute(
                select(EvidencePassage, Source)
                .join(Source, Source.id == EvidencePassage.source_id)
                .where(EvidencePassage.investigation_id == investigation_id)
                .order_by(EvidencePassage.id)
            )
        ).all()
    return [
        pl.PassageData(
            label=p.label,
            text=p.text or "",
            lang=p.lang,
            source_url=s.canonical_url,
            domain=s.domain,
            tier=s.tier,
            publisher=s.publisher,
            title=s.title,
            published_at=s.published_at,
            simhash=p.simhash,
            score=p.score,
            round=p.round,
            position=p.position,
            origin=p.origin,
            translation_fr=p.translation_fr,
            db_id=p.id,
        )
        for p, s in rows
    ]


def _date(state: _State) -> date | None:
    return state.post_dt.date() if state.post_dt else None


# ------------------------------------------------------------------------ advancing


async def _locked(
    session: AsyncSession, investigation_id: int, expected: _STAGES
) -> Investigation | None:
    inv = await session.scalar(
        select(Investigation)
        .where(Investigation.id == investigation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if inv is None or inv.stage != expected.value:
        return None
    return inv


def _merge_flags(
    inv: Investigation,
    flags: Mapping[str, Any] | None,
    steps: Sequence[pl.Step[Any]],
    refused: str | None,
) -> dict[str, Any]:
    merged: dict[str, Any] = dict(inv.flags or {})
    merged.update(flags or {})
    versions = {
        "prompt": dict((merged.get("versions") or {}).get("prompt", {})),
        "route": dict((merged.get("versions") or {}).get("route", {})),
    }
    for step in steps:
        versions["prompt"][step.role] = step.prompt_version
        versions["route"][step.role] = step.route_version
    merged["versions"] = versions
    if refused is not None:
        merged["refused"] = sorted({*merged.get("refused", []), refused})
    return merged


async def _apply(
    session: AsyncSession,
    inv: Investigation,
    *,
    stage: _STAGES,
    cost: int = 0,
    sets: Mapping[str, Any] | None = None,
    flags: Mapping[str, Any] | None = None,
    steps: Sequence[pl.Step[Any]] = (),
    refused: str | None = None,
    next_job: tuple[str, int] | None = None,
) -> None:
    """Bookkeeping of a stage transition (the caller holds the lock)."""
    for key, value in (sets or {}).items():
        setattr(inv, key, value)
    inv.flags = _merge_flags(inv, flags, steps, refused)
    inv.cost_usd_micros = inv.cost_usd_micros + cost
    inv.stage = stage.value
    inv.updated_at = datetime.now(UTC)
    if next_job is not None:
        kind, round_ = next_job
        await enqueue(
            session,
            names.FC_DEEP,
            kind,
            {"investigation_id": inv.id, "round": round_},
            dedupe_key=names.stage_key(_STAGE_OF_JOB[kind], inv.claim_group_id, inv.id, round_),
        )


async def _advance(
    ctx: JobContext,
    state: _State,
    *,
    expect: _STAGES,
    stage: _STAGES,
    cost: int = 0,
    sets: Mapping[str, Any] | None = None,
    flags: Mapping[str, Any] | None = None,
    steps: Sequence[pl.Step[Any]] = (),
    refused: str | None = None,
    next_job: tuple[str, int] | None = None,
) -> bool:
    async with ctx.db.transaction() as session:
        inv = await _locked(session, state.inv.id, expect)
        if inv is None:
            return False
        await _apply(
            session,
            inv,
            stage=stage,
            cost=cost,
            sets=sets,
            flags=flags,
            steps=steps,
            refused=refused,
            next_job=next_job,
        )
    return True


def _req[T](value: T | None) -> T:
    """A stage output the previous stage must have stored."""
    if value is None:
        raise PermanentError("investigation state is missing a previous stage output")
    return value


def _dump(model: Any) -> dict[str, Any]:
    out: dict[str, Any] = model.model_dump(mode="json")
    return out


def _last_attempt(ctx: JobContext) -> bool:
    return ctx.job.attempts >= ctx.job.max_attempts


# --------------------------------------------------------------------- job runner


Step = Callable[[JobContext, _State], Awaitable[None]]
_OPTIONAL_STAGES = frozenset({_STAGES.SECOND_OPINION, _STAGES.OBJECTION, _STAGES.JUDGING})


async def _run(ctx: JobContext, expect: _STAGES, step: Step) -> None:
    state = await _load(ctx, expect)
    if state is None:
        return
    try:
        await step(ctx, state)
    except (RetryLater, PermanentError):
        raise
    except BudgetExhausted as exc:
        if _last_attempt(ctx) and expect in _OPTIONAL_STAGES and state.inv.judgement is not None:
            log.warning("investigate.budget_incomplete", investigation=state.inv.id)
            await _advance(
                ctx,
                state,
                expect=expect,
                stage=_STAGES.DRAFTING,
                flags={"budget_incomplete": True},
                next_job=(names.JOB_DRAFT, state.inv.round),
            )
            return
        raise RetryLater(_clamped_delay(exc), str(exc)) from exc
    except ProviderNotConfigured as exc:
        await _fail(ctx, state, expect, exc)
        raise PermanentError("model provider not configured") from exc
    except ProviderUnavailable as exc:
        raise to_job_error(exc) from exc
    except Exception as exc:
        if _last_attempt(ctx):
            await _fail(ctx, state, expect, exc)
            raise PermanentError(f"{expect.value} failed: {type(exc).__name__}") from exc
        raise


def _clamped_delay(exc: BudgetExhausted) -> float:
    retry_at = exc.retry_at if exc.retry_at.tzinfo else exc.retry_at.replace(tzinfo=UTC)
    delay = (retry_at - datetime.now(UTC)).total_seconds()
    return min(max(delay, _MIN_RETRY_S), _MAX_RETRY_S)


async def _fail(ctx: JobContext, state: _State, stage: _STAGES, exc: BaseException) -> None:
    capture(
        ec.FCK_STAGE_FAILED,
        exc,
        stage=stage.value,
        investigation_id=state.inv.id,
        error_type=type(exc).__name__,
    )
    async with ctx.db.transaction() as session:
        await uc.fail_investigation(
            session,
            ACTOR,
            state.inv,
            error=f"{stage.value}: {type(exc).__name__}: {exc}",
        )


# ------------------------------------------------------------------ subscription


async def _resolve(session: AsyncSession, raw: object) -> tuple[int, int] | None:
    """``(investigation id, group id)`` from a public UUID or an internal id."""
    stmt = select(Investigation.id, Investigation.claim_group_id)
    try:
        stmt = stmt.where(Investigation.public_id == uuid.UUID(str(raw)))
    except ValueError:
        try:
            stmt = stmt.where(Investigation.id == int(str(raw)))
        except ValueError:
            return None
    row = (await session.execute(stmt)).first()
    return None if row is None else (int(row[0]), int(row[1]))


async def on_requested(session: AsyncSession, events: Sequence[EventRecord]) -> None:
    """``investigation.requested`` → job ``investigate.frame`` (short: no model call)."""
    for event in events:
        found = await _resolve(session, event.payload.get("investigation_id"))
        if found is None:
            log.warning("investigate.request_unknown", event_id=event.id)
            continue
        inv_id, group_id = found
        await enqueue(
            session,
            names.FC_DEEP,
            names.JOB_FRAME,
            {"investigation_id": inv_id},
            dedupe_key=names.stage_key("frame", group_id, inv_id, 1),
        )


# ------------------------------------------------------------------------- stages


async def frame(ctx: JobContext) -> None:
    await _run(ctx, _STAGES.FRAMING, _frame)


async def _frame(ctx: JobContext, st: _State) -> None:
    inv = st.inv
    llm = runtime.get_llm(ctx.settings, ctx.db)
    try:
        step = await pl.call_frame(
            llm, st.view, level=inv.level, depth=inv.depth, today=st.today, post_date=_date(st)
        )
    except AllRefused as exc:
        role = pl.frame_role(inv.depth)
        capture(ec.FCK_MODEL_REFUSED, exc, role=role, investigation_id=inv.id)
        await _advance(
            ctx,
            st,
            expect=_STAGES.FRAMING,
            stage=_STAGES.DRAFTING,
            sets={"judgement": _dump(pl.synthetic_judgement(pl.REFUSED_FRAME_POINT))},
            flags={"unverifiable_at_framing": True, "synthetic_judgement": True},
            refused=role,
            next_job=(names.JOB_DRAFT, inv.round),
        )
        return
    framing: FrameOutput = step.output
    unverifiable = not framing.verifiable or st.view.claim_type in {
        t.value for t in UNVERIFIABLE_CLAIM_TYPES
    }
    if not unverifiable:
        await _advance(
            ctx,
            st,
            expect=_STAGES.FRAMING,
            stage=_STAGES.COLLECTING,
            cost=step.cost_usd_micros,
            sets={"framing": _dump(framing)},
            steps=[step],
            next_job=(names.JOB_COLLECT, inv.round),
        )
        return
    if inv.level != "N4":
        async with ctx.db.transaction() as session:
            locked = await _locked(session, inv.id, _STAGES.FRAMING)
            if locked is None:
                return
            await uc.complete_investigation(
                session,
                ACTOR,
                locked,
                outcome="unverifiable_at_framing",
                verdict=None,
                cost_usd_micros=step.cost_usd_micros,
                updates={"framing": _dump(framing)},
                flags=_merge_flags(locked, {"unverifiable_at_framing": True}, [step], None),
            )
        return
    # N4: the reviewers decide, so a draft "unverifiable" is produced without a note.
    await _advance(
        ctx,
        st,
        expect=_STAGES.FRAMING,
        stage=_STAGES.DRAFTING,
        cost=step.cost_usd_micros,
        sets={
            "framing": _dump(framing),
            "judgement": _dump(pl.synthetic_judgement(framing.verifiability_reason)),
        },
        flags={"unverifiable_at_framing": True, "synthetic_judgement": True},
        steps=[step],
        next_job=(names.JOB_DRAFT, inv.round),
    )


async def collect(ctx: JobContext) -> None:
    await _run(ctx, _STAGES.COLLECTING, _collect)


def _bytes_or_none(value: str | None) -> bytes | None:
    if value is None:
        return None
    try:
        return bytes.fromhex(value)
    except ValueError:
        return None


async def _collect(ctx: JobContext, st: _State) -> None:
    inv = st.inv
    framing = FrameOutput.model_validate(_req(inv.framing))
    llm = runtime.get_llm(ctx.settings, ctx.db)
    atlas = await runtime.get_atlas(ctx.settings, ctx.db)
    existing = await _load_passages(ctx.db, inv.id) if inv.round > 1 else []
    rounds: list[dict[str, Any]] = list((inv.queries or {}).get("rounds", []))
    previous = [
        q["query"] for r in rounds for side in ("confirm", "refute") for q in r["queries"][side]
    ]
    outcome = await pl.collect_round(
        llm,
        atlas,
        ctx.settings,
        st.view,
        framing,
        level=inv.level,
        depth=inv.depth,
        round_=inv.round,
        existing=existing,
        previous_queries=previous,
        today=st.today,
        post_date=_date(st),
    )
    no_evidence = not existing and not outcome.passages
    async with ctx.db.transaction() as session:
        locked = await _locked(session, inv.id, _STAGES.COLLECTING)
        if locked is None:
            return
        sources = await _upsert_sources(session, outcome)
        for p in outcome.passages:
            source = sources[p.source_url]
            session.add(
                EvidencePassage(
                    investigation_id=inv.id,
                    source_id=source.id,
                    label=p.label,
                    position=p.position,
                    text=p.text,
                    lang=p.lang,
                    simhash=p.simhash,
                    score=p.score,
                    round=p.round,
                )
            )
        sets: dict[str, Any] = {"queries": {"rounds": [*rounds, outcome.record]}}
        flags: dict[str, Any] = {}
        if outcome.asymmetric:
            flags["asymmetric_queries"] = True
        if no_evidence:
            sets["judgement"] = _dump(
                pl.synthetic_judgement(needs_more_research=pl.NO_EVIDENCE_POINT)
            )
            flags.update({"no_evidence": True, "synthetic_judgement": True})
            stage, job = _STAGES.DRAFTING, names.JOB_DRAFT
        else:
            stage, job = _STAGES.JUDGING, names.JOB_JUDGE
        await _apply(
            session,
            locked,
            stage=stage,
            cost=outcome.cost_usd_micros,
            sets=sets,
            flags=flags,
            steps=[outcome.queries],
            next_job=(job, inv.round),
        )


async def _upsert_sources(session: AsyncSession, outcome: pl.CollectOutcome) -> dict[str, Source]:
    """Insert the retrieved sources (the first row of a URL keeps its data)."""
    by_url = {d.canonical_url: d for d in outcome.atlas.documents}
    for url, doc in by_url.items():
        await session.execute(
            pg_insert(Source)
            .values(
                canonical_url=url,
                original_url=doc.original_url,
                adapter=doc.adapter,
                stable_id=doc.stable_id,
                publisher=doc.publisher,
                domain=doc.domain,
                tier=doc.tier,
                title=doc.title,
                lang=doc.lang,
                published_at=doc.published_at,
                sha256=_bytes_or_none(doc.sha256),
                license=doc.license,
            )
            .on_conflict_do_nothing(index_elements=["canonical_url"])
        )
    if not by_url:
        return {}
    rows = (
        await session.scalars(select(Source).where(Source.canonical_url.in_(list(by_url))))
    ).all()
    return {s.canonical_url: s for s in rows}


async def judge(
    ctx: JobContext,
) -> None:
    await _run(ctx, _STAGES.JUDGING, _judge)


def _second_pass_flags(first: JudgeOutput, final: JudgeOutput) -> dict[str, Any]:
    return {"second_pass": "concordant" if first.veracity == final.veracity else "discordant"}


async def _judge(ctx: JobContext, st: _State) -> None:
    inv = st.inv
    framing = FrameOutput.model_validate(_req(inv.framing))
    llm = runtime.get_llm(ctx.settings, ctx.db)
    passages = await _load_passages(ctx.db, inv.id)
    second_pass = inv.round == 2
    slot = "second_pass" if second_pass else "judgement"
    role = pl.judge_role(inv.depth)
    try:
        step = await pl.call_judge(
            llm,
            st.view,
            framing,
            pl.judge_views(passages, second_pass=second_pass),
            level=inv.level,
            depth=inv.depth,
            today=st.today,
            post_date=_date(st),
        )
    except AllRefused as exc:
        capture(ec.FCK_MODEL_REFUSED, exc, role=role, investigation_id=inv.id)
        await _advance(
            ctx,
            st,
            expect=_STAGES.JUDGING,
            stage=_STAGES.DRAFTING,
            sets={slot: _dump(pl.synthetic_judgement(pl.REFUSED_JUDGE_POINT))},
            flags={"synthetic_judgement": True},
            refused=role,
            next_job=(names.JOB_DRAFT, inv.round),
        )
        return
    if not second_pass:
        await _advance(
            ctx,
            st,
            expect=_STAGES.JUDGING,
            stage=_STAGES.SECOND_OPINION,
            cost=step.cost_usd_micros,
            sets={"judgement": _dump(step.output)},
            steps=[step],
            next_job=(names.JOB_SECOND_OPINION, inv.round),
        )
        return
    first = JudgeOutput.model_validate(_req(inv.judgement))
    await _advance(
        ctx,
        st,
        expect=_STAGES.JUDGING,
        stage=_STAGES.DRAFTING,
        cost=step.cost_usd_micros,
        sets={"second_pass": _dump(step.output)},
        flags=_second_pass_flags(first, step.output),
        steps=[step],
        next_job=(names.JOB_DRAFT, inv.round),
    )


async def second_opinion(ctx: JobContext) -> None:
    await _run(ctx, _STAGES.SECOND_OPINION, _second_opinion)


async def _second_opinion(ctx: JobContext, st: _State) -> None:
    inv = st.inv
    framing = FrameOutput.model_validate(_req(inv.framing))
    first = JudgeOutput.model_validate(_req(inv.judgement))
    llm = runtime.get_llm(ctx.settings, ctx.db)
    passages = await _load_passages(ctx.db, inv.id)
    try:
        step = await pl.call_second_opinion(
            llm,
            st.view,
            framing,
            pl.judge_views(passages, second_pass=False),
            level=inv.level,
            today=st.today,
            post_date=_date(st),
        )
    except AllRefused as exc:
        capture(ec.FCK_MODEL_REFUSED, exc, role=names.ROLE_SECOND_OPINION, investigation_id=inv.id)
        await _advance(
            ctx,
            st,
            expect=_STAGES.SECOND_OPINION,
            stage=_STAGES.ESCALATING,
            refused=names.ROLE_SECOND_OPINION,
            next_job=(names.JOB_ESCALATE, inv.round),
        )
        return
    agreement = compare_opinions(first.veracity, step.output.veracity)
    substantive = agreement is Agreement.SUBSTANTIVE
    await _advance(
        ctx,
        st,
        expect=_STAGES.SECOND_OPINION,
        stage=_STAGES.OBJECTION if substantive else _STAGES.ESCALATING,
        cost=step.cost_usd_micros,
        sets={"second_opinion": _dump(step.output), "agreement": agreement.value},
        flags={
            "second_opinion_veracity": step.output.veracity.value,
            "agreement": agreement.value,
        },
        steps=[step],
        next_job=(names.JOB_OBJECTION if substantive else names.JOB_ESCALATE, inv.round),
    )


async def objection(ctx: JobContext) -> None:
    await _run(ctx, _STAGES.OBJECTION, _objection)


async def _objection(ctx: JobContext, st: _State) -> None:
    inv = st.inv
    framing = FrameOutput.model_validate(_req(inv.framing))
    first = JudgeOutput.model_validate(_req(inv.judgement))
    second = SecondOpinionOutput.model_validate(_req(inv.second_opinion))
    llm = runtime.get_llm(ctx.settings, ctx.db)
    passages = await _load_passages(ctx.db, inv.id)
    try:
        step = await pl.call_objection(
            llm,
            st.view,
            framing,
            pl.judge_views(passages, second_pass=False),
            first,
            second,
            level=inv.level,
            today=st.today,
            post_date=_date(st),
        )
    except AllRefused as exc:
        capture(ec.FCK_MODEL_REFUSED, exc, role=names.ROLE_OBJECTION, investigation_id=inv.id)
        await _advance(
            ctx,
            st,
            expect=_STAGES.OBJECTION,
            stage=_STAGES.ESCALATING,
            refused=names.ROLE_OBJECTION,
            next_job=(names.JOB_ESCALATE, inv.round),
        )
        return
    out: ObjectionOutput = step.output
    await _advance(
        ctx,
        st,
        expect=_STAGES.OBJECTION,
        stage=_STAGES.ESCALATING,
        cost=step.cost_usd_micros,
        sets={"objection": _dump(out)},
        flags={
            "objection": {
                "type": out.objection_type.value,
                "direction": out.direction.value,
                "explanation": out.explanation,
                "label": out.passage_id,
            }
        },
        steps=[step],
        next_job=(names.JOB_ESCALATE, inv.round),
    )


async def escalate(ctx: JobContext) -> None:
    await _run(ctx, _STAGES.ESCALATING, _escalate)


async def _escalate(ctx: JobContext, st: _State) -> None:
    inv = st.inv
    first = JudgeOutput.model_validate(_req(inv.judgement))
    passages = await _load_passages(ctx.db, inv.id)
    framing = FrameOutput.model_validate(inv.framing) if inv.framing else None
    agreement = Agreement(inv.agreement) if inv.agreement else None
    assessment = pl.assess_judgement(first, passages)
    confidence, _ = pl.confidence_of(
        ctx.settings,
        claim_type=st.view.claim_type,
        framing=framing,
        judge=first,
        assessment=assessment,
        agreement=agreement,
        second_pass=None,
    )
    route = pl.decide_route(
        ctx.settings,
        level=inv.level,
        depth=inv.depth,
        round_=inv.round,
        agreement=agreement,
        conflicting_sources=first.conflicting_sources,
        confidence=confidence,
        spent_usd_micros=inv.cost_usd_micros,
    )
    flags: dict[str, Any] = {}
    if route.cap_reached:
        flags["cap_reached"] = True
        capture(
            ec.FCK_INVESTIGATION_CAP,
            investigation_id=inv.id,
            spent_usd_micros=inv.cost_usd_micros,
            level=inv.level,
        )
    if route.action == "escalate":
        await _advance(
            ctx,
            st,
            expect=_STAGES.ESCALATING,
            stage=_STAGES.COLLECTING,
            sets={"depth": "reinforced", "round": 2},
            flags={"escalated": True},
            next_job=(names.JOB_COLLECT, 2),
        )
    elif route.action == "second_pass":
        await _advance(
            ctx,
            st,
            expect=_STAGES.ESCALATING,
            stage=_STAGES.JUDGING,
            sets={"round": 2},
            flags=flags,
            next_job=(names.JOB_JUDGE, 2),
        )
    else:
        await _advance(
            ctx,
            st,
            expect=_STAGES.ESCALATING,
            stage=_STAGES.DRAFTING,
            flags=flags,
            next_job=(names.JOB_DRAFT, inv.round),
        )


def _by_db_id(by_label: Mapping[str, pl.PassageData], values: Mapping[str, str]) -> dict[int, str]:
    out: dict[int, str] = {}
    for label, value in values.items():
        db_id = by_label[label].db_id
        if db_id is not None:
            out[db_id] = value
    return out


async def draft(ctx: JobContext) -> None:
    await _run(ctx, _STAGES.DRAFTING, _draft)


def _retrieved_urls(inv: Investigation, passages: Sequence[pl.PassageData]) -> list[str]:
    urls = [p.source_url for p in passages]
    for r in (inv.queries or {}).get("rounds", []):
        urls.extend(r.get("urls", []))
    return urls


async def _draft(ctx: JobContext, st: _State) -> None:
    inv = st.inv
    final = inv.second_pass or inv.judgement
    judge_out = JudgeOutput.model_validate(_req(final))
    flags = dict(inv.flags or {})
    passages = await _load_passages(ctx.db, inv.id)
    extra: list[str] = []
    if inv.objection:
        extra.append(ObjectionOutput.model_validate(inv.objection).explanation)
    if flags.get("second_pass") == "discordant" and inv.judgement and inv.second_pass:
        extra.append(
            "Les deux passes du juge divergent : "
            f"{JudgeOutput.model_validate(inv.judgement).veracity.value} puis "
            f"{JudgeOutput.model_validate(inv.second_pass).veracity.value}."
        )
    base_flags = {k: v for k, v in flags.items() if k not in _INTERNAL_FLAGS}
    base_flags["depth"] = inv.depth
    base_flags["round"] = inv.round
    base_flags["agreement"] = inv.agreement
    base_flags.setdefault("escalated", False)
    if inv.objection is None:
        base_flags.setdefault("objection", None)
    llm = runtime.get_llm(ctx.settings, ctx.db)
    claim_view_ = st.view
    outcome = await pl.compose_draft(
        llm,
        ctx.settings,
        claim=claim_view_,
        level=inv.level,
        judge=judge_out,
        synthetic=bool(flags.get("synthetic_judgement")),
        passages=passages,
        flags=base_flags,
        extra_review_points=extra,
        post_text=st.content.text if st.content is not None else st.claim.text_original,
        post_lang=st.content.lang if st.content is not None else st.claim.lang_original,
        post_datetime=st.post_dt,
        today=st.today,
        retrieved_urls=_retrieved_urls(inv, passages),
        last_attempt=_last_attempt(ctx),
        investigation_id=inv.id,
        versions=flags.get("versions"),
    )
    by_label = {p.label: p for p in passages}
    citations = [
        uc.CitationFields(
            passage_id=c.passage.db_id,
            quote=c.quote,
            assertion=c.assertion,
            valid=c.valid,
            position=c.position,
        )
        for c in outcome.citations
        if c.passage is not None and c.passage.db_id is not None
    ]
    note = None
    if outcome.notes:
        first = outcome.notes[0]
        note = uc.NoteFields(
            text=first.text,
            lang=first.lang,
            links=outcome.links,
            primary_context_type=outcome.primary_context_type,
            checks=outcome.checks.as_dict(),
            critic=outcome.flags.get("critic"),
            translations=[
                uc.NoteTranslationFields(lang=n.lang, text=n.text, fact_sheet=n.fact_sheet)
                for n in outcome.notes
            ],
        )
    verdict_flags = dict(outcome.flags)
    verdict_flags["checks"] = outcome.checks.as_dict()
    inv_flags = dict(flags)
    inv_flags["versions"] = outcome.versions
    async with ctx.db.transaction() as session:
        locked = await _locked(session, inv.id, _STAGES.DRAFTING)
        if locked is None:
            return
        await uc.complete_investigation(
            session,
            ACTOR,
            locked,
            outcome="drafted",
            verdict=uc.VerdictFields(
                veracity=outcome.veracity.value,
                context_need=outcome.context_need.value,
                context_types=outcome.context_types,
                rationale=outcome.rationale,
                not_in_sources=outcome.not_in_sources,
                review_points=outcome.review_points,
                flags=verdict_flags,
                prompt_versions=outcome.versions["prompt"],
                route_versions=outcome.versions["route"],
            ),
            citations=citations,
            note=note,
            passage_translations=_by_db_id(by_label, outcome.passage_translations),
            passage_origins=_by_db_id(by_label, outcome.origins),
            cost_usd_micros=outcome.cost_usd_micros,
            flags=inv_flags,
        )
