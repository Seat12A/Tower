"""E5 light check (lot 5): a cheap preliminary signal on a new group, never published.

Two paths. A numeric claim with a validated reference fact is decided by its value band, no
model involved. Otherwise a small model compares the claim with short evidence items: the
existing fact-checks and structured sources of the Atlas (no encyclopedia, no web search),
or the kept passages of the evidence dossier of a surging narrative. The result only feeds
the priority (``claim_groups.light_signal``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain import error_codes as ec
from tower.core.domain.factcheck.datamark import datamark
from tower.core.domain.factcheck.scheduling import light_signal_from_band
from tower.core.domain.factcheck.values import LightSignal
from tower.core.domain.llm import Level
from tower.core.domain.security import Actor
from tower.core.errors import capture
from tower.core.factcheck import inputs, names, runtime
from tower.core.factcheck.candidates import claim_signature, post_dates
from tower.core.factcheck.profiles import active_profile
from tower.core.factcheck.references import band_for, resolve_fact
from tower.core.factcheck.schemas import LightCheckOutput
from tower.core.jobs.queue import PermanentError, RetryLater
from tower.core.llm.errors import BudgetExhausted, LLMRouterError
from tower.core.logging import get_logger
from tower.core.models.factcheck import Claim, ClaimGroup, EvidencePassage, Source
from tower.core.ports.llm import LLMRequest
from tower.core.services.base import JobContext
from tower.core.usecases import scheduling as uc

__all__ = ["light_check"]

log = get_logger(__name__)

_MAX_TEXT = 600
_MAX_EVIDENCE = 6
_MAX_TOKENS = 400
_MIN_RETRY_S = 60.0
_MAX_RETRY_S = 6 * 3600.0
_FACT_CHECK_ADAPTER = "factcheck_tools"


def _actor() -> Actor:
    return Actor.system(names.FACTCHECK_ACTOR)


def _num(value: Any) -> float | None:
    return None if value is None else float(value)


def _iso(value: Any) -> str | None:
    return None if value is None else value.isoformat()


def _claim_view(claim: Claim) -> inputs.ClaimView:
    """What the model knows about the claim: the claims row, never the post or its author."""
    return inputs.ClaimView(
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


def _retry_delay(retry_at: datetime) -> float:
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    delay = (retry_at - datetime.now(UTC)).total_seconds()
    return min(max(delay, _MIN_RETRY_S), _MAX_RETRY_S)


def _item(n: int, kind: str, source: str, text: str) -> dict[str, str]:
    return {
        "id": f"E{n}",
        "kind": kind,
        "source": source,
        "text": datamark(text.strip()[:_MAX_TEXT]),
    }


def _already_done(group: ClaimGroup, version: int, dossier: int | None) -> bool:
    """Idempotence: one check per (group, profile version, dossier)."""
    evidence = group.light_evidence
    return (
        group.light_checked_at is not None
        and evidence is not None
        and evidence.get("profile_version") == version
        and evidence.get("dossier_investigation_id") == dossier
    )


async def _dossier_evidence(session: AsyncSession, investigation_id: int) -> list[dict[str, str]]:
    """Kept passages (or passages whose text is still there) of an investigation."""
    rows = (
        await session.execute(
            select(EvidencePassage.text, EvidencePassage.translation_fr, Source.domain)
            .join(Source, Source.id == EvidencePassage.source_id)
            .where(
                EvidencePassage.investigation_id == investigation_id,
                (EvidencePassage.kept.is_(True)) | (EvidencePassage.text.is_not(None)),
            )
            .order_by(EvidencePassage.kept.desc(), EvidencePassage.score.desc(), EvidencePassage.id)
            .limit(_MAX_EVIDENCE)
        )
    ).all()
    items: list[dict[str, str]] = []
    for text, translation, domain in rows:
        body = text or translation
        if body:
            items.append(_item(len(items) + 1, "dossier_passage", str(domain), str(body)))
    return items


async def _atlas_evidence(
    ctx: JobContext, claim: Claim, post_date: date | None
) -> list[dict[str, str]]:
    """Existing fact-checks and structured sources: no encyclopedia, no web search."""
    from tower.core.adapters.knowledge.atlas import AtlasRequest

    atlas = await runtime.get_atlas(ctx.settings, ctx.db)
    result = await atlas.collect(
        AtlasRequest(
            claim_text=claim.text_original,
            claim_lang=claim.lang_original,
            claim_type=claim.claim_type,
            jurisdictions=tuple(claim.jurisdictions or ()),
            queries=(),
            entity_lookups=tuple(str(e["name"]) for e in (claim.entities or ()) if e.get("name")),
            max_web_searches=0,
            as_of=post_date,
            max_documents=_MAX_EVIDENCE,
            max_reference_fetches=0,
            skip_encyclopedia=True,
        )
    )
    items: list[dict[str, str]] = []
    for doc in result.documents[:_MAX_EVIDENCE]:
        if not doc.text.strip():
            continue
        kind = "fact_check" if doc.adapter == _FACT_CHECK_ADAPTER else "structured_source"
        items.append(_item(len(items) + 1, kind, doc.domain, doc.text))
    return items


async def _write(
    ctx: JobContext,
    group_id: int,
    signal: LightSignal,
    *,
    explanation: str,
    evidence: Sequence[Mapping[str, str]],
    version: int,
    dossier: int | None,
) -> None:
    async with ctx.db.transaction() as session:
        await uc.record_light_signal(
            session,
            _actor(),
            group_id,
            signal,
            explanation=explanation,
            evidence=evidence,
            now=datetime.now(UTC),
            profile_version=version,
            dossier_investigation_id=dossier,
        )
    log.info("light_check.done", group=group_id, signal=signal.value)


async def light_check(ctx: JobContext) -> None:
    """Job ``factcheck.light_check``. Payload ``{"group_id", "dossier_investigation_id"}``."""
    payload = ctx.job.payload
    try:
        group_id = int(payload["group_id"])
        raw = payload.get("dossier_investigation_id")
        dossier = None if raw is None else int(raw)
    except (KeyError, TypeError, ValueError) as exc:
        raise PermanentError("bad light_check payload") from exc

    async with ctx.db.session() as session:
        version = (await active_profile(session, ctx.settings)).version
        group = await session.get(ClaimGroup, group_id)
        if group is None or group.state == "merged":
            log.info("light_check.skipped", group=group_id)
            return
        if _already_done(group, version, dossier):
            return
        claim = await session.get(Claim, group.representative_claim_id)
        if claim is None:
            raise PermanentError("group without representative claim")
        level = group.level
        post_date = (await post_dates(session, [claim.id])).get(claim.id)

        # Numeric claim against a validated fact: the band decides, no model.
        sig = claim_signature(claim, post_date)
        fact = await resolve_fact(session, sig, post_date=post_date)
        if fact is not None:
            band, _side, _outdated = await band_for(session, sig, fact, ctx.settings)
            fact_evidence = [{"id": "F1", "kind": "reference_fact", "source": fact.source_name}]
            fact_explanation = (
                f"Claimed value is {band.value} against validated fact {fact.indicator}."
            )
        else:
            band = None
            fact_evidence, fact_explanation = [], ""
        evidence_items = await _dossier_evidence(session, dossier) if dossier is not None else []
    if band is not None:
        await _write(
            ctx,
            group_id,
            light_signal_from_band(band),
            explanation=fact_explanation,
            evidence=fact_evidence,
            version=version,
            dossier=dossier,
        )
        return

    try:
        if dossier is None:
            evidence_items = await _atlas_evidence(ctx, claim, post_date)
    except Exception as exc:  # the lookup is optional: a failure leaves the group unchecked
        capture(ec.FCK_LIGHT_CHECK_FAILED, exc, group=group_id, stage="evidence")
        return
    if not evidence_items:
        await _write(
            ctx,
            group_id,
            LightSignal.UNCERTAIN,
            explanation="No evidence found.",
            evidence=[],
            version=version,
            dossier=dossier,
        )
        return

    request = LLMRequest(
        role=names.ROLE_LIGHT_CHECK,
        level=Level(level),
        prompt_id=names.prompt_id(names.ROLE_LIGHT_CHECK),
        input=inputs.light_check_input(
            claim=_claim_view(claim), post_date=post_date, evidence=evidence_items
        ),
        schema=LightCheckOutput,
        max_output_tokens=_MAX_TOKENS,
        budget_scope=names.level_scope(level),
    )
    try:
        result = await runtime.get_llm(ctx.settings, ctx.db).complete(request)
    except BudgetExhausted as exc:
        raise RetryLater(_retry_delay(exc.retry_at), "model budget exhausted") from exc
    except LLMRouterError as exc:
        capture(ec.FCK_LIGHT_CHECK_FAILED, exc, group=group_id, stage="model")
        return
    output = result.output
    if not isinstance(output, LightCheckOutput):
        capture(ec.FCK_LIGHT_CHECK_FAILED, message="Unexpected light check output type")
        return
    known = {item["id"]: item for item in evidence_items}
    cited = [
        {"id": i, "kind": known[i]["kind"], "source": known[i]["source"]}
        for i in output.evidence_ids
        if i in known
    ]
    await _write(
        ctx,
        group_id,
        output.signal,
        explanation=output.explanation,
        evidence=cited,
        version=version,
        dossier=dossier,
    )
