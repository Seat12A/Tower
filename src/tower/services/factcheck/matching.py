"""E3 orchestration: embeddings, nearest groups, arbiter, fact and band resolution.

Runs outside any database transaction that spans a network call: every read is a short
session of its own. The decisions are applied by ``claims.record_extraction`` in one
transaction. The author, its account and its reach never enter this module.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date
from typing import Any

from sqlalchemy import or_, select

from tower.core.domain import error_codes as ec
from tower.core.domain.factcheck.datamark import datamark
from tower.core.domain.factcheck.matching import Candidate, MatchDecision, plan_match
from tower.core.domain.factcheck.signatures import NormalizedSignature
from tower.core.domain.factcheck.values import ArbiterReason
from tower.core.domain.llm import Level
from tower.core.errors import capture
from tower.core.factcheck import inputs, names, runtime
from tower.core.factcheck.candidates import (
    NearRow,
    build_candidates,
    nearest_groups,
    signature_from_extracted,
)
from tower.core.factcheck.profiles import active_profile
from tower.core.factcheck.references import band_for, resolve_fact
from tower.core.factcheck.schemas import ExtractedClaim, PairArbiterOutput
from tower.core.llm.errors import BudgetExhausted, LLMRouterError
from tower.core.logging import get_logger
from tower.core.models.factcheck import Claim
from tower.core.ports.llm import LLMRequest
from tower.core.services.base import JobContext

__all__ = ["match_claims"]

log = get_logger(__name__)

_ARBITER_MAX_TOKENS = 300
_MAX_EXTERNAL = 3
_RATING = re.compile(r": (.+?) \([^()]*\)$")
_PIVOT_LANG = "fr"


# ------------------------------------------------------------------------ embeddings


async def _embed_all(
    ctx: JobContext, claims: Sequence[ExtractedClaim], *, lang: str, level: str, content_id: int
) -> tuple[dict[str, list[float]], str]:
    """Vector of every text to embed (originals, plus the French text of non-French
    claims). Vectors already stored for the same text and model are reused."""
    texts: list[str] = []
    for claim in claims:
        for text in (claim.text_original, None if lang == _PIVOT_LANG else claim.text_fr):
            if text and text not in texts:
                texts.append(text)
    embedder = runtime.get_embedder(ctx.settings, ctx.db)
    model = embedder.model_name
    found: dict[str, list[float]] = {}
    async with ctx.db.session() as session:
        rows = (
            await session.execute(
                select(
                    Claim.text_original,
                    Claim.text_fr,
                    Claim.embedding,
                    Claim.embedding_pivot,
                ).where(
                    or_(Claim.text_original.in_(texts), Claim.text_fr.in_(texts)),
                    Claim.embedding_model == model,
                )
            )
        ).all()
    for original, french, vector, pivot in rows:
        if vector is not None and original in texts:
            found.setdefault(original, vector)
        if pivot is not None and french in texts:
            found.setdefault(french, pivot)
    missing = [t for t in texts if t not in found]
    if missing:
        result = await embedder.embed(missing, level=level, ref=f"factcheck:match:{content_id}")
        for text, vector in zip(missing, result.vectors, strict=True):
            found[text] = vector
        model = result.model
    return found, model


# ------------------------------------------------------------------------- arbiter


async def _arbiter_says_same(
    ctx: JobContext,
    *,
    level: str,
    claim_text: str,
    claim_lang: str,
    claim_date: date | None,
    row: NearRow,
) -> tuple[bool, str | None]:
    """One ``pair_arbiter`` call. Honoured only for ``same`` with reason ``same_claim``;
    any failure is "different" (``BudgetExhausted`` propagates)."""
    request = LLMRequest(
        role=names.ROLE_PAIR_ARBITER,
        level=Level(level),
        prompt_id=names.prompt_id(names.ROLE_PAIR_ARBITER),
        input=inputs.pair_arbiter_input(
            a_text=datamark(claim_text),
            a_lang=claim_lang,
            a_date=claim_date,
            b_text=datamark(row.claim_text),
            b_lang=row.claim_lang,
            b_date=row.post_date,
        ),
        schema=PairArbiterOutput,
        max_output_tokens=_ARBITER_MAX_TOKENS,
        budget_scope=names.level_scope(level),
    )
    llm = runtime.get_llm(ctx.settings, ctx.db)
    try:
        result = await llm.complete(request)
    except BudgetExhausted:
        raise
    except LLMRouterError as exc:
        capture(ec.FCK_ARBITER_FAILED, exc, group=row.group_id)
        return False, None
    output = result.output
    if not isinstance(output, PairArbiterOutput):  # pragma: no cover (typed schema)
        capture(ec.FCK_ARBITER_FAILED, message="Unexpected arbiter output type")
        return False, None
    if output.same and output.reason is ArbiterReason.SAME_CLAIM:
        return True, output.reason.value
    return False, output.reason.value


# ------------------------------------------------------------- external fact-checks


async def _external_factchecks(
    ctx: JobContext, claim: ExtractedClaim, lang: str
) -> tuple[Mapping[str, str], ...]:
    """Existing fact-checks (Google Fact Check Tools through the Atlas, cached by
    ``source_cache``). Unavailable or not configured: empty, never fatal."""
    try:
        from tower.core.adapters.knowledge.atlas import AtlasRequest

        atlas = await runtime.get_atlas(ctx.settings, ctx.db)
        result = await atlas.collect(
            AtlasRequest(
                claim_text=claim.text_original,
                claim_lang=lang,
                claim_type="other",
                jurisdictions=(),
                queries=(),
                entity_lookups=(),
                max_web_searches=0,
                max_documents=_MAX_EXTERNAL,
                max_reference_fetches=0,
                fact_checks_only=True,
            )
        )
    except Exception as exc:  # external lookup must never fail the extraction
        log.warning("factcheck.external_lookup_failed", error=type(exc).__name__)
        return ()
    out: list[Mapping[str, str]] = []
    for doc in result.documents:
        if doc.adapter != "factcheck_tools":
            continue
        match = _RATING.search(doc.text)
        out.append(
            {
                "url": doc.original_url,
                "publisher": doc.publisher or "",
                "rating": match.group(1) if match else "n/a",
                "title": doc.title or "",
            }
        )
        if len(out) >= _MAX_EXTERNAL:
            break
    return tuple(out)


# ----------------------------------------------------------------------- main entry


async def _decide(
    ctx: JobContext,
    *,
    claim: ExtractedClaim,
    signature: NormalizedSignature,
    lang: str,
    level: str,
    post_date: date | None,
    vector: list[float],
    pivot: list[float],
    same_threshold: float,
    grey_threshold: float,
) -> MatchDecision:
    settings = ctx.settings
    async with ctx.db.session() as session:
        rows = await nearest_groups(session, pivot, limit=settings.fc_match_candidates)
    by_id = {row.group_id: row for row in rows}
    candidates: list[Candidate] = build_candidates(
        rows,
        claim_lang=lang,
        signature=signature,
        original_embedding=vector,
        current_period_days=settings.fc_current_period_days,
    )
    by_candidate = {c.group_id: c for c in candidates}

    def similarity(group_id: int) -> float:
        cand = by_candidate[group_id]
        same_lang = cand.claim_lang.casefold() == lang.casefold()
        if same_lang and cand.similarity_original is not None:
            return cand.similarity_original
        return cand.similarity_pivot

    plan = plan_match(
        lang, candidates, same_threshold=same_threshold, grey_threshold=grey_threshold
    )
    if plan.join_without_arbiter is not None:
        gid = plan.join_without_arbiter
        return MatchDecision.join(gid, "identical_signature:similarity", similarity=similarity(gid))
    last_reason: str | None = None
    for gid in plan.arbiter_queue:
        same, reason = await _arbiter_says_same(
            ctx,
            level=level,
            claim_text=claim.text_original,
            claim_lang=lang,
            claim_date=post_date,
            row=by_id[gid],
        )
        last_reason = reason or last_reason
        if same:
            return MatchDecision.join(
                gid,
                "identical_signature:arbiter",
                similarity=similarity(gid),
                arbiter_reason=reason,
            )
    related: int | None = None
    if plan.related_candidates:
        gid = plan.related_candidates[0]
        same, reason = await _arbiter_says_same(
            ctx,
            level=level,
            claim_text=claim.text_original,
            claim_lang=lang,
            claim_date=post_date,
            row=by_id[gid],
        )
        last_reason = reason or last_reason
        if same:
            related = gid
    if related is not None:
        return MatchDecision.new(
            "new:related",
            similarity=similarity(related),
            arbiter_reason=last_reason,
            related_group_id=related,
        )
    if not candidates:
        return MatchDecision.new("new:no_candidate")
    if plan.arbiter_queue or plan.related_candidates:
        return MatchDecision.new("new:arbiter_different", arbiter_reason=last_reason)
    first = next(iter(plan.rejected.values()), "no_match")
    return MatchDecision.new(f"new:{first}")


async def match_claims(
    ctx: JobContext,
    *,
    content_id: int,
    post_date: date | None,
    lang: str,
    claims: Sequence[ExtractedClaim],
    level: str,
) -> list[MatchDecision]:
    """One ``MatchDecision`` per claim, same order. ``BudgetExhausted`` (embeddings or
    arbiter) propagates: the job is retried later."""
    if not claims:
        return []
    settings = ctx.settings
    vectors, model = await _embed_all(ctx, claims, lang=lang, level=level, content_id=content_id)
    async with ctx.db.session() as session:
        profile = await active_profile(session, settings)
    pivot_language = lang == _PIVOT_LANG
    decisions: list[MatchDecision] = []
    for claim in claims:
        vector = vectors[claim.text_original]
        pivot = vector if pivot_language else vectors.get(claim.text_fr, vector)
        signature = signature_from_extracted(claim, post_date)
        decision = await _decide(
            ctx,
            claim=claim,
            signature=signature,
            lang=lang,
            level=level,
            post_date=post_date,
            vector=vector,
            pivot=pivot,
            same_threshold=profile.match_same_threshold,
            grey_threshold=profile.match_grey_threshold,
        )
        extras: dict[str, Any] = {}
        if decision.group_id is None:
            async with ctx.db.session() as session:
                fact = await resolve_fact(session, signature, post_date=post_date)
                if fact is not None:
                    band, side, outdated = await band_for(session, signature, fact, settings)
                    extras.update(
                        reference_fact_id=fact.id, band=band, band_side=side, outdated=outdated
                    )
            if settings.fc_external_factcheck_lookup:
                extras["external_factchecks"] = await _external_factchecks(ctx, claim, lang)
        decisions.append(
            _with(
                decision,
                embedding=tuple(vector),
                embedding_pivot=None if pivot_language else tuple(pivot),
                embedding_model=model,
                **extras,
            )
        )
    log.info(
        "factcheck.matched",
        content=content_id,
        claims=len(claims),
        joined=sum(1 for d in decisions if d.group_id is not None),
    )
    return decisions


def _with(decision: MatchDecision, **changes: Any) -> MatchDecision:
    return replace(decision, **changes)
