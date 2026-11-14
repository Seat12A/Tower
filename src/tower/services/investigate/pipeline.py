"""Pure step functions of the deep investigation (pipeline.md E6 to E9).

Nothing here touches the database: the job handlers (``jobs.py``) load the state, call
these functions and persist the outcome, and the evaluation tool calls ``run_in_memory``,
so the evaluation measures the production logic. Model calls go through an ``LLMClient``;
the router itself writes ``llm_calls`` and budgets.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from datetime import time as dt_time
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from tower.core.adapters.knowledge.atlas import (
    AtlasDocument,
    AtlasQuery,
    AtlasRequest,
    AtlasResult,
)
from tower.core.config import Settings
from tower.core.domain import error_codes as ec
from tower.core.domain.factcheck.atlas import is_stakeholder
from tower.core.domain.factcheck.checks import (
    NOTE_MAX_CHARS,
    CheckReport,
    DatedSource,
    check_dates,
    check_links,
    find_loaded_terms,
    note_length,
    quote_in_passage,
)
from tower.core.domain.factcheck.confidence import (
    ConfidenceBand,
    band,
    compute_confidence,
)
from tower.core.domain.factcheck.datamark import unmark
from tower.core.domain.factcheck.independence import (
    IndependenceResult,
    SourceRef,
    apply_independence_rule,
    assess_independence,
)
from tower.core.domain.factcheck.opinion import (
    EscalationInput,
    compare_opinions,
    initial_depth,
    should_escalate,
)
from tower.core.domain.factcheck.passages import (
    Chunk,
    RankedChunk,
    bm25_rank,
    chunk_text,
    select_passages,
)
from tower.core.domain.factcheck.profiles import Profile
from tower.core.domain.factcheck.values import (
    CONTEXT_REGIME,
    UNVERIFIABLE_CLAIM_TYPES,
    Agreement,
    ClaimType,
    ContextNeed,
    ContextType,
    Depth,
    Regime,
    Veracity,
)
from tower.core.domain.llm import Level
from tower.core.domain.text import hamming
from tower.core.errors import capture
from tower.core.factcheck import inputs, names, runtime
from tower.core.factcheck.inputs import ClaimView, PassageView, SourceView
from tower.core.factcheck.publishers import PublisherLookup
from tower.core.factcheck.schemas import (
    CriticOutput,
    FrameOutput,
    JudgeOutput,
    ObjectionOutput,
    QueryOutput,
    SecondOpinionOutput,
    TranslateOutput,
    WriterNote,
    WriterOutput,
)
from tower.core.factcheck.signals import build_signals
from tower.core.llm.errors import AllRefused, BudgetExhausted, NoValidOutput
from tower.core.logging import get_logger
from tower.core.ports.llm import LLMClient, LLMRequest

log = get_logger(__name__)

__all__ = [
    "NO_EVIDENCE_POINT",
    "REFUSED_FRAME_POINT",
    "REFUSED_JUDGE_POINT",
    "Assessment",
    "AtlasLike",
    "CitationCheck",
    "CollectOutcome",
    "DraftOutcome",
    "InMemoryOutcome",
    "PassageData",
    "Route",
    "Step",
    "assess_judgement",
    "atlas_cap",
    "atlas_request",
    "call_critic",
    "call_frame",
    "call_judge",
    "call_objection",
    "call_queries",
    "call_second_opinion",
    "call_translate",
    "call_writer",
    "cap_micros",
    "collect_round",
    "compose_draft",
    "confidence_of",
    "decide_route",
    "judge_views",
    "passage_views",
    "run_in_memory",
    "synthetic_judgement",
    "writer_languages",
]

REFUSED_JUDGE_POINT = "Le modèle juge a refusé de traiter cette affirmation."
REFUSED_FRAME_POINT = "Le modèle de cadrage a refusé de traiter cette affirmation."
NO_EVIDENCE_POINT = "Aucune source récupérée."

_MAX_TOKENS = {
    "frame": 3000,
    "query": 1500,
    "judge": 6000,
    "second_opinion": 1500,
    "objection": 1000,
    "writer": 3000,
    "critic": 1000,
    "translate": 1500,
}
_DUPLICATE_BITS = 3
_PROVISIONAL_MAX_AGE = timedelta(days=2)


class AtlasLike(Protocol):
    async def collect(self, request: AtlasRequest) -> AtlasResult: ...


# --------------------------------------------------------------------------- steps


@dataclass(frozen=True, slots=True)
class Step[T: BaseModel]:
    """Parsed output of one model call with what the investigation records about it."""

    output: T
    role: str
    cost_usd_micros: int
    prompt_version: str
    route_version: int
    model: str


async def _call[T: BaseModel](
    llm: LLMClient,
    *,
    role: str,
    level: str,
    schema: type[T],
    payload: Mapping[str, Any],
    max_tokens: int,
) -> Step[T]:
    result = await llm.complete(
        LLMRequest(
            role=role,
            level=Level(level),
            prompt_id=names.prompt_id(role),
            input=payload,
            schema=schema,
            max_output_tokens=max_tokens,
            budget_scope=names.level_scope(level),
        )
    )
    output = result.output
    if not isinstance(output, schema):
        raise NoValidOutput(f"Role {role} returned an unexpected output type")
    return Step(
        output=output,
        role=role,
        cost_usd_micros=result.cost_usd_micros,
        prompt_version=result.prompt_version,
        route_version=result.route_version,
        model=result.model,
    )


def frame_role(depth: Depth | str) -> str:
    return names.ROLE_FRAME_REINFORCED if depth == Depth.REINFORCED else names.ROLE_FRAME


def judge_role(depth: Depth | str) -> str:
    return names.ROLE_JUDGE_REINFORCED if depth == Depth.REINFORCED else names.ROLE_JUDGE


async def call_frame(
    llm: LLMClient,
    claim: ClaimView,
    *,
    level: str,
    depth: Depth | str,
    today: date,
    post_date: date | None,
) -> Step[FrameOutput]:
    return await _call(
        llm,
        role=frame_role(depth),
        level=level,
        schema=FrameOutput,
        payload=inputs.frame_input(claim, today=today, post_date=post_date),
        max_tokens=_MAX_TOKENS["frame"],
    )


async def call_queries(
    llm: LLMClient,
    settings: Settings,
    claim: ClaimView,
    framing: FrameOutput,
    *,
    level: str,
    depth: Depth | str,
    round_: int,
    today: date,
    previous_queries: Sequence[str] = (),
) -> Step[QueryOutput]:
    payload = inputs.query_input(
        claim,
        framing.model_dump(mode="json"),
        today=today,
        max_per_side=4 if depth == Depth.STANDARD and round_ == 1 else 6,
        source_languages=_dedupe([claim.lang, *settings.fc_reference_lang_list, "en"]),
    )
    if previous_queries:
        # Changes the cache key too: a round 2 must not replay the round 1 queries.
        payload["round"] = round_
        payload["previous_queries"] = list(previous_queries)
    return await _call(
        llm,
        role=names.ROLE_QUERY_WRITER,
        level=level,
        schema=QueryOutput,
        payload=payload,
        max_tokens=_MAX_TOKENS["query"],
    )


async def call_judge(
    llm: LLMClient,
    claim: ClaimView,
    framing: FrameOutput,
    views: Sequence[PassageView],
    *,
    level: str,
    depth: Depth | str,
    today: date,
    post_date: date | None,
) -> Step[JudgeOutput]:
    """``views`` are given in the order the judge must read them."""
    return await _call(
        llm,
        role=judge_role(depth),
        level=level,
        schema=JudgeOutput,
        payload=inputs.judge_input(
            claim, framing.model_dump(mode="json"), views, today=today, post_date=post_date
        ),
        max_tokens=_MAX_TOKENS["judge"],
    )


async def call_second_opinion(
    llm: LLMClient,
    claim: ClaimView,
    framing: FrameOutput,
    views: Sequence[PassageView],
    *,
    level: str,
    today: date,
    post_date: date | None,
) -> Step[SecondOpinionOutput]:
    """``views`` in the judge's order (the input builder reverses them)."""
    return await _call(
        llm,
        role=names.ROLE_SECOND_OPINION,
        level=level,
        schema=SecondOpinionOutput,
        payload=inputs.second_opinion_input(
            claim, framing.model_dump(mode="json"), views, today=today, post_date=post_date
        ),
        max_tokens=_MAX_TOKENS["second_opinion"],
    )


async def call_objection(
    llm: LLMClient,
    claim: ClaimView,
    framing: FrameOutput,
    views: Sequence[PassageView],
    judge: JudgeOutput,
    second: SecondOpinionOutput,
    *,
    level: str,
    today: date,
    post_date: date | None,
) -> Step[ObjectionOutput]:
    return await _call(
        llm,
        role=names.ROLE_OBJECTION,
        level=level,
        schema=ObjectionOutput,
        payload=inputs.objection_input(
            claim,
            framing.model_dump(mode="json"),
            views,
            {
                "veracity": judge.veracity.value,
                "context_need": judge.context_need.value,
                "rationale": [a.model_dump(mode="json") for a in judge.rationale],
            },
            {"veracity": second.veracity.value, "reason": second.reason},
            today=today,
            post_date=post_date,
        ),
        max_tokens=_MAX_TOKENS["objection"],
    )


async def call_writer(
    llm: LLMClient,
    claim: ClaimView,
    verdict: Mapping[str, Any],
    sources: Sequence[SourceView],
    *,
    level: str,
    post_text: str,
    post_lang: str,
    post_date: date | None,
    languages: Sequence[str],
    checked_at: datetime,
) -> Step[WriterOutput]:
    return await _call(
        llm,
        role=names.ROLE_WRITER,
        level=level,
        schema=WriterOutput,
        payload=inputs.writer_input(
            claim,
            verdict,
            sources,
            post_text=post_text,
            post_lang=post_lang,
            post_date=post_date,
            languages=languages,
            checked_at=checked_at,
        ),
        max_tokens=_MAX_TOKENS["writer"],
    )


async def call_critic(
    llm: LLMClient,
    claim: ClaimView,
    verdict: Mapping[str, Any],
    notes: Sequence[Mapping[str, str]],
    *,
    level: str,
) -> Step[CriticOutput]:
    return await _call(
        llm,
        role=names.ROLE_CRITIC,
        level=level,
        schema=CriticOutput,
        payload=inputs.critic_input(claim, verdict, notes),
        max_tokens=_MAX_TOKENS["critic"],
    )


async def call_translate(
    llm: LLMClient, *, level: str, text: str, source_lang: str, target_lang: str, kind: str
) -> Step[TranslateOutput]:
    return await _call(
        llm,
        role=names.ROLE_TRANSLATE,
        level=level,
        schema=TranslateOutput,
        payload=inputs.translate_input(
            text=text, source_lang=source_lang, target_lang=target_lang, kind=kind
        ),
        max_tokens=_MAX_TOKENS["translate"],
    )


# ------------------------------------------------------------------------ helpers


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def writer_languages(settings: Settings, post_lang: str) -> list[str]:
    """Post language first, then the reference languages (``en`` for English posts)."""
    return _dedupe(
        [post_lang, *settings.fc_reference_lang_list, *(["en"] if post_lang == "en" else [])]
    )


def synthetic_judgement(*review_points: str, needs_more_research: str | None = None) -> JudgeOutput:
    """Judgement used when no model judged: never a true/false verdict."""
    return JudgeOutput(
        veracity=Veracity.UNVERIFIABLE,
        context_need=ContextNeed.NONE,
        context=[],
        rationale=[],
        origins=[],
        conflicting_sources=False,
        not_in_sources=[],
        review_points=list(review_points),
        needs_more_research=needs_more_research,
    )


def cap_micros(
    settings: Settings, level: str, depth: Depth | str, profile: Profile | None = None
) -> int:
    """Per-investigation cost cap (micro-dollars) of the level and depth. With the active
    ``profile``, the standard depth and N4 use the level's unit cap; the reinforced depth
    outside N4 keeps ``fc_cap_reinforced_usd``."""
    if level == "N4":
        usd = settings.fc_cap_n4_usd if profile is None else profile.level("N4").unit_cap_usd
    elif depth == Depth.REINFORCED:
        usd = settings.fc_cap_reinforced_usd
    elif profile is not None:
        usd = profile.level(level).unit_cap_usd
    else:
        usd = settings.fc_cap_standard_usd
    return round(usd * 1_000_000)


def atlas_cap(settings: Settings) -> int:
    """Hard ceiling used to test the pure escalation condition (reinforced cap)."""
    return round(settings.fc_cap_reinforced_usd * 1_000_000)


# ------------------------------------------------------------------------ passages


@dataclass(slots=True)
class PassageData:
    """A passage with its source data (loaded from the database or freshly collected)."""

    label: str
    text: str
    lang: str
    source_url: str
    domain: str
    tier: str
    publisher: str | None
    title: str | None
    published_at: datetime | None
    simhash: int
    score: float
    round: int
    position: int = 0
    origin: str | None = None
    translation_fr: str | None = None
    db_id: int | None = None
    document: AtlasDocument | None = None
    """Set on freshly collected passages (the job persists the source from it)."""


def passage_views(passages: Sequence[PassageData]) -> dict[str, PassageView]:
    """``PassageView`` by label; sources are numbered ``S1``… by first appearance in
    label order (stable across replays)."""
    ordered = sorted(passages, key=_label_number)
    source_ids: dict[str, SourceView] = {}
    views: dict[str, PassageView] = {}
    for p in ordered:
        source = source_ids.get(p.source_url)
        if source is None:
            source = SourceView(
                id=f"S{len(source_ids) + 1}",
                url=p.source_url,
                publisher=p.publisher,
                domain=p.domain,
                tier=p.tier,
                title=p.title,
                published_at=p.published_at,
            )
            source_ids[p.source_url] = source
        views[p.label] = PassageView(
            id=p.label,
            source=source,
            lang=p.lang,
            text=p.text,
            translation_fr=p.translation_fr,
        )
    return views


def _label_number(p: PassageData) -> int:
    return int(p.label[1:]) if p.label[1:].isdigit() else 0


def judge_views(passages: Sequence[PassageData], *, second_pass: bool) -> list[PassageView]:
    """Judge order: round 1 by descending score; the second pass reads every passage in
    reversed label order."""
    views = passage_views(passages)
    if second_pass:
        return [views[p.label] for p in sorted(passages, key=_label_number, reverse=True)]
    ordered = sorted(passages, key=lambda p: (p.round != 1, -p.score, _label_number(p)))
    return [views[p.label] for p in ordered if p.round == 1] or [views[p.label] for p in ordered]


def _query_terms(claim: ClaimView, framing: FrameOutput, queries: QueryOutput) -> list[str]:
    terms = [claim.text_original, claim.text_fr]
    terms += [q.question for q in framing.sub_questions]
    terms += [q.query for q in (*queries.confirm, *queries.refute)]
    return terms


def atlas_request(
    claim: ClaimView,
    queries: QueryOutput,
    *,
    settings: Settings,
    depth: Depth | str,
    round_: int,
    as_of: date | None,
) -> AtlasRequest:
    items = tuple(
        AtlasQuery(
            query=q.query,
            lang=q.lang,
            source_type=q.source_type,
            target=q.target,
            side=side,
        )
        for side, series in (("confirm", queries.confirm), ("refute", queries.refute))
        for q in series
    )
    if round_ == 1:
        searches = settings.fc_searches_standard + (
            settings.fc_searches_reinforced_extra if depth == Depth.REINFORCED else 0
        )
    else:
        searches = settings.fc_searches_reinforced_extra
    return AtlasRequest(
        claim_text=claim.text_original,
        claim_lang=claim.lang,
        claim_type=claim.claim_type,
        jurisdictions=claim.jurisdictions,
        queries=items,
        entity_lookups=tuple(queries.entity_lookups),
        max_web_searches=searches,
        as_of=as_of,
    )


@dataclass(slots=True)
class CollectOutcome:
    queries: Step[QueryOutput]
    atlas: AtlasResult
    passages: list[PassageData]
    """New passages, labelled after ``existing`` ones."""
    record: dict[str, Any]
    """JSON kept in ``investigations.queries["rounds"]``."""
    asymmetric: bool
    cost_usd_micros: int
    """Query writer plus paid Atlas calls."""


def select_new_passages(
    claim: ClaimView,
    framing: FrameOutput,
    queries: QueryOutput,
    documents: Sequence[AtlasDocument],
    existing: Sequence[PassageData],
    *,
    settings: Settings,
    round_: int,
    reinforced: bool,
    registry_tier: Callable[[str], tuple[str, str | None]] | None = None,
) -> list[PassageData]:
    """Chunk, rank (BM25) and select the passages of ``documents``; round 2 appends to
    ``existing`` up to the reinforced limit, skipping near-duplicates of existing ones."""
    by_url: dict[str, AtlasDocument] = {}
    for doc in documents:
        by_url.setdefault(doc.canonical_url, doc)
    chunks: list[Chunk] = []
    for url, doc in by_url.items():
        chunks.extend(chunk_text(url, doc.text, target_words=settings.fc_passage_words))
    ranked: list[RankedChunk] = bm25_rank(_query_terms(claim, framing, queries), chunks)
    if round_ == 1:
        limit = settings.fc_passages_reinforced if reinforced else settings.fc_passages_standard
    else:
        limit = settings.fc_passages_reinforced - len(existing)
        known = [p.simhash for p in existing]
        ranked = [
            r
            for r in ranked
            if all(hamming(r.simhash, k) > _DUPLICATE_BITS for k in known)
            and not any(
                p.source_url == r.chunk.source_key and p.text == r.chunk.text for p in existing
            )
        ]
    selected = select_passages(ranked, limit=limit)
    out: list[PassageData] = []
    for i, item in enumerate(selected):
        doc = by_url[item.chunk.source_key]
        tier, publisher = (doc.tier, doc.publisher)
        if registry_tier is not None:
            tier, registry_publisher = registry_tier(doc.canonical_url)
            publisher = publisher or registry_publisher
        out.append(
            PassageData(
                label=f"P{len(existing) + i + 1}",
                text=item.chunk.text,
                lang=doc.lang,
                source_url=doc.canonical_url,
                domain=doc.domain,
                tier=tier,
                publisher=publisher,
                title=doc.title,
                published_at=doc.published_at,
                simhash=item.simhash,
                score=item.score,
                round=round_,
                position=item.chunk.position,
                document=doc,
            )
        )
    return out


async def collect_round(
    llm: LLMClient,
    atlas: AtlasLike,
    settings: Settings,
    claim: ClaimView,
    framing: FrameOutput,
    *,
    level: str,
    depth: Depth | str,
    round_: int,
    existing: Sequence[PassageData],
    previous_queries: Sequence[str],
    today: date,
    post_date: date | None,
    registry_tier: Callable[[str], tuple[str, str | None]] | None = None,
) -> CollectOutcome:
    """E7: write the symmetric queries, collect through Atlas, select the passages."""
    step = await call_queries(
        llm,
        settings,
        claim,
        framing,
        level=level,
        depth=depth,
        round_=round_,
        today=today,
        previous_queries=previous_queries,
    )
    queries = step.output
    asymmetric = not queries.confirm or not queries.refute
    if asymmetric:
        log.warning(
            "investigate.asymmetric_queries",
            confirm=len(queries.confirm),
            refute=len(queries.refute),
        )
    request = atlas_request(
        claim, queries, settings=settings, depth=depth, round_=round_, as_of=post_date
    )
    result = await atlas.collect(request)
    passages = select_new_passages(
        claim,
        framing,
        queries,
        result.documents,
        existing,
        settings=settings,
        round_=round_,
        reinforced=depth == Depth.REINFORCED,
        registry_tier=registry_tier,
    )
    urls = _dedupe(u for d in result.documents for u in (d.canonical_url, d.original_url))
    record = {
        "round": round_,
        "queries": queries.model_dump(mode="json"),
        "skipped": dict(result.skipped),
        "adapters": list(result.adapters_used),
        "web_searches": result.web_searches,
        "documents": len(result.documents),
        "passages": len(passages),
        "urls": urls,
    }
    return CollectOutcome(
        queries=step,
        atlas=result,
        passages=passages,
        record=record,
        asymmetric=asymmetric,
        cost_usd_micros=step.cost_usd_micros + result.paid_cost_usd_micros,
    )


# ------------------------------------------------------------------ judgement checks


@dataclass(frozen=True, slots=True)
class CitationCheck:
    position: int
    assertion_index: int
    assertion: str
    label: str
    quote: str
    valid: bool
    passage: PassageData | None


@dataclass(slots=True)
class Assessment:
    """Citations and independence of a judgement, and its veracity after the rule."""

    veracity: Veracity
    context_need: ContextNeed
    downgraded: bool
    citations: list[CitationCheck]
    refs: list[SourceRef]
    independence: IndependenceResult
    primary_labels: list[str]
    """Passages the judge marked as primary (existing passages only)."""

    @property
    def valid_labels(self) -> list[str]:
        return _dedupe(c.label for c in self.citations if c.valid)

    @property
    def invalid(self) -> list[CitationCheck]:
        return [c for c in self.citations if not c.valid]


def assess_judgement(
    judge: JudgeOutput,
    passages: Sequence[PassageData],
    *,
    claim: ClaimView | None = None,
    publishers: PublisherLookup | None = None,
) -> Assessment:
    """Validate every citation against the ORIGINAL passage text, then apply the
    independence rule (pipeline.md E9). With ``claim`` and ``publishers`` (registry), the
    closed stakeholder rule (E7, lot 6) marks the passages whose publisher, or its
    controller, is a party to a record/conduct/accusation claim: they do not count."""
    parties = (
        [(str(e.get("name", "")), str(e.get("role", ""))) for e in claim.entities]
        if claim is not None
        else []
    )

    def stakeholder(p: PassageData) -> bool:
        if claim is None or publishers is None:
            return False
        return is_stakeholder(claim.claim_type, parties, publishers(p.source_url))

    by_label = {p.label: p for p in passages}
    citations: list[CitationCheck] = []
    position = 0
    for index, assertion in enumerate(judge.rationale):
        for citation in assertion.citations:
            passage = by_label.get(citation.passage_id)
            valid = passage is not None and quote_in_passage(
                unmark(citation.quote), unmark(passage.text)
            )
            citations.append(
                CitationCheck(
                    position=position,
                    assertion_index=index,
                    assertion=assertion.assertion,
                    label=citation.passage_id,
                    quote=citation.quote,
                    valid=valid,
                    passage=passage,
                )
            )
            position += 1
    origin_of = {o.passage_id: o.origin for o in judge.origins}
    valid_labels = _dedupe(c.label for c in citations if c.valid)
    refs = [
        SourceRef(
            passage_id=label,
            origin=origin_of.get(label),
            domain=by_label[label].domain,
            tier=by_label[label].tier,
            simhash=by_label[label].simhash,
            stakeholder=stakeholder(by_label[label]),
        )
        for label in valid_labels
    ]
    independence = assess_independence(refs)
    veracity, need, downgraded = apply_independence_rule(
        judge.veracity,
        judge.context_need,
        independence,
        has_partial_evidence=bool(valid_labels),
    )
    primary = [o.passage_id for o in judge.origins if o.primary and o.passage_id in by_label]
    return Assessment(
        veracity=veracity,
        context_need=need,
        downgraded=downgraded,
        citations=citations,
        refs=refs,
        independence=independence,
        primary_labels=_dedupe(primary),
    )


def confidence_of(
    settings: Settings,
    *,
    claim_type: str,
    framing: FrameOutput | None,
    judge: JudgeOutput,
    assessment: Assessment,
    agreement: Agreement | None,
    second_pass: bool | None,
) -> tuple[float, ConfidenceBand]:
    signals = build_signals(
        veracity=assessment.veracity,
        claim_type=claim_type,
        pitfalls=[p.value for p in framing.known_pitfalls] if framing else [],
        context_types=[c.context_type.value for c in judge.context],
        cited=assessment.refs,
        primary_passage_ids=assessment.primary_labels,
        independence=assessment.independence,
        conflicting_sources=judge.conflicting_sources,
        agreement=agreement,
        second_pass=second_pass,
    )
    coefficients, thresholds = runtime.confidence_settings(settings)
    value = compute_confidence(signals, coefficients, thresholds)
    return value, band(value, thresholds)


# --------------------------------------------------------------------------- routing


@dataclass(frozen=True, slots=True)
class Route:
    action: Literal["escalate", "second_pass", "draft"]
    cap_reached: bool = False


def decide_route(
    settings: Settings,
    *,
    level: str,
    depth: Depth | str,
    round_: int,
    agreement: Agreement | None,
    conflicting_sources: bool,
    confidence: float,
    spent_usd_micros: int,
    profile: Profile | None = None,
) -> Route:
    """Escalation step: switch to the reinforced depth, run a second judge pass without new
    collection (N4), or go to the draft. Never exceeds the cost cap of the depth."""
    cap = cap_micros(settings, level, depth, profile)
    if depth == Depth.STANDARD:
        if round_ != 1:
            return Route("draft")
        base = EscalationInput(
            level=level,
            depth=Depth.STANDARD,
            agreement=agreement or Agreement.IDENTICAL,
            conflicting_sources=conflicting_sources,
            confidence=confidence,
            spent_usd_micros=spent_usd_micros,
            reinforced_cap_usd_micros=atlas_cap(settings),
            threshold=settings.fc_escalation_threshold,
        )
        if not should_escalate(replace(base, spent_usd_micros=0)):
            return Route("draft")
        if spent_usd_micros >= cap or not should_escalate(base):
            return Route("draft", cap_reached=True)
        return Route("escalate")
    if round_ == 1 and (level == "N4" or agreement is Agreement.SUBSTANTIVE):
        if spent_usd_micros < cap:
            return Route("second_pass")
        return Route("draft", cap_reached=True)
    return Route("draft")


# ----------------------------------------------------------------------------- draft


@dataclass(slots=True)
class DraftOutcome:
    veracity: Veracity
    context_need: ContextNeed
    context_types: list[str]
    rationale: list[dict[str, Any]]
    citations: list[CitationCheck]
    not_in_sources: list[str]
    review_points: list[str]
    flags: dict[str, Any]
    notes: list[WriterNote]
    links: list[str]
    primary_context_type: ContextType | None
    checks: CheckReport
    critic: CriticOutput | None
    passage_translations: dict[str, str]
    """Passage label → French translation (cited passages not written in fr/en)."""
    origins: dict[str, str]
    """Passage label → origin text, for every passage the judge listed."""
    assessment: Assessment
    versions: dict[str, dict[str, Any]] = field(default_factory=lambda: {"prompt": {}, "route": {}})
    cost_usd_micros: int = 0
    refused: list[str] = field(default_factory=list)


def _record_version(versions: dict[str, dict[str, Any]], step: Step[Any]) -> None:
    versions["prompt"][step.role] = step.prompt_version
    versions["route"][step.role] = step.route_version


def _unique_refused(roles: Iterable[str]) -> list[str]:
    return _dedupe(roles)


async def compose_draft(
    llm: LLMClient,
    settings: Settings,
    *,
    claim: ClaimView,
    level: str,
    judge: JudgeOutput,
    synthetic: bool,
    passages: Sequence[PassageData],
    flags: Mapping[str, Any],
    extra_review_points: Sequence[str],
    post_text: str,
    post_lang: str,
    post_datetime: datetime | None,
    today: date,
    retrieved_urls: Iterable[str],
    last_attempt: bool,
    investigation_id: int | None = None,
    versions: Mapping[str, Mapping[str, Any]] | None = None,
    checked_at: datetime | None = None,
    publishers: PublisherLookup | None = None,
) -> DraftOutcome:
    """E9: validate the citations, apply the independence rule, write the note, run the
    deterministic checks and the critic, translate the cited foreign passages.

    ``synthetic``: the judgement was not produced by a model (no evidence, refusal,
    unverifiable at framing): no note is written. A budget error propagates, except on the
    ``last_attempt`` where the remaining optional steps are skipped and flagged.

    ``checked_at`` (default: now) is shown in the note; the evaluation pins it to its
    ``today`` so a rerun hits the model-call cache (a wall-clock minute changed the writer
    prompt between two runs)."""
    out_flags: dict[str, Any] = dict(flags)
    assessment = assess_judgement(judge, passages, claim=claim, publishers=publishers)
    stakeholders = [r.passage_id for r in assessment.refs if r.stakeholder]
    if stakeholders:
        out_flags["stakeholder_passages"] = stakeholders
    for bad in assessment.invalid:
        capture(
            ec.FCK_CITATION_INVALID,
            label=bad.label,
            investigation_id=investigation_id,
        )
    by_label = {p.label: p for p in passages}
    invalid_labels = _dedupe(c.label for c in assessment.invalid)

    # Rationale displayed: valid citations only; an assertion without any is dropped.
    rationale: list[dict[str, Any]] = []
    for index, assertion in enumerate(judge.rationale):
        kept = [
            {
                "passage_id": c.passage.db_id if c.passage else None,
                "label": c.label,
                "quote": c.quote,
                "valid": True,
            }
            for c in assessment.citations
            if c.valid and c.assertion_index == index
        ]
        if kept:
            rationale.append({"assertion": assertion.assertion, "citations": kept})

    valid_points = []
    for point in judge.context:
        ids = [i for i in point.passage_ids if i in assessment.valid_labels]
        valid_points.append((point, ids))
    context_types = _dedupe(p.context_type.value for p in judge.context)

    out_flags["downgraded"] = assessment.downgraded
    out_flags["independent_origins"] = assessment.independence.independent_origins
    out_flags["has_primary"] = assessment.independence.has_primary
    out_flags["primary_passages"] = list(assessment.primary_labels)
    out_flags["conflicting_sources"] = judge.conflicting_sources

    review_points = [*judge.review_points, *extra_review_points]
    not_in_sources = list(judge.not_in_sources)
    spent = 0
    refused = list(out_flags.get("refused", []))
    vers: dict[str, dict[str, Any]] = {
        "prompt": dict((versions or {}).get("prompt", {})),
        "route": dict((versions or {}).get("route", {})),
    }

    checks = CheckReport(invalid_quotes=invalid_labels, downgraded=assessment.downgraded)
    notes: list[WriterNote] = []
    links: list[str] = []
    primary_context: ContextType | None = None
    critic: CriticOutput | None = None
    translations: dict[str, str] = {}

    def budget_stop(exc: BudgetExhausted) -> None:
        if not last_attempt:
            raise exc
        out_flags["budget_incomplete"] = True

    stop = False
    verdict_view: dict[str, Any] = {
        "veracity": assessment.veracity.value,
        "context_need": assessment.context_need.value,
        "context": [
            {
                "context_type": p.context_type.value,
                "element": p.element,
                "passage_ids": ids,
            }
            for p, ids in valid_points
        ],
        "rationale": [],
    }
    cited_sources = _cited_sources(assessment, by_label)
    source_of_url = {s.url: s for s in cited_sources}
    # The writer and the critic see the quotes with the source number, not database ids.
    verdict_view["rationale"] = [
        {
            "assertion": entry["assertion"],
            "citations": [
                {
                    "passage_id": c["label"],
                    "source_id": source_of_url[by_label[c["label"]].source_url].id,
                    "quote": c["quote"],
                }
                for c in entry["citations"]
            ],
        }
        for entry in rationale
    ]

    if not synthetic and assessment.valid_labels:
        languages = writer_languages(settings, post_lang)
        try:
            writer = await call_writer(
                llm,
                claim,
                verdict_view,
                cited_sources,
                level=level,
                post_text=post_text,
                post_lang=post_lang,
                post_date=post_datetime.date() if post_datetime else None,
                languages=languages,
                checked_at=checked_at or datetime.now(UTC),
            )
        except AllRefused as exc:
            capture(
                ec.FCK_MODEL_REFUSED, exc, role=names.ROLE_WRITER, investigation_id=investigation_id
            )
            refused.append(names.ROLE_WRITER)
        except BudgetExhausted as exc:
            budget_stop(exc)
            stop = True
        else:
            spent += writer.cost_usd_micros
            _record_version(vers, writer)
            notes = _keep_languages(writer.output.notes, languages)
            links = list(writer.output.links)
            primary_context = writer.output.primary_context_type

    if notes:
        checks = _run_checks(
            settings,
            notes=notes,
            links=links,
            checks=checks,
            cited=[by_label[label] for label in assessment.valid_labels],
            retrieved_urls=list(retrieved_urls),
            post_datetime=post_datetime,
            claim_type=claim.claim_type,
            context_types=context_types,
        )
        links = [u for u in links if u not in checks.unknown_links]
        if not stop:
            try:
                critique = await call_critic(
                    llm,
                    claim,
                    {
                        "veracity": assessment.veracity.value,
                        "context_need": assessment.context_need.value,
                        "rationale": verdict_view["rationale"],
                    },
                    [{"lang": n.lang, "text": n.text} for n in notes],
                    level=level,
                )
            except AllRefused as exc:
                capture(
                    ec.FCK_MODEL_REFUSED,
                    exc,
                    role=names.ROLE_CRITIC,
                    investigation_id=investigation_id,
                )
                refused.append(names.ROLE_CRITIC)
            except BudgetExhausted as exc:
                budget_stop(exc)
                stop = True
            else:
                spent += critique.cost_usd_micros
                _record_version(vers, critique)
                critic = critique.output

    if not stop and not synthetic:
        for label in assessment.valid_labels:
            passage = by_label[label]
            if passage.lang in ("fr", "en") or passage.translation_fr:
                continue
            try:
                translated = await call_translate(
                    llm,
                    level=level,
                    text=passage.text,
                    source_lang=passage.lang,
                    target_lang="fr",
                    kind="passage",
                )
            except AllRefused as exc:
                capture(
                    ec.FCK_MODEL_REFUSED,
                    exc,
                    role=names.ROLE_TRANSLATE,
                    investigation_id=investigation_id,
                )
                refused.append(names.ROLE_TRANSLATE)
                continue
            except BudgetExhausted as exc:
                budget_stop(exc)
                break
            spent += translated.cost_usd_micros
            _record_version(vers, translated)
            translations[label] = translated.output.text

    if refused:
        out_flags["refused"] = _unique_refused(refused)
    out_flags["checks"] = checks.as_dict()
    out_flags["critic"] = critic.model_dump(mode="json") if critic else None
    origins = {o.passage_id: o.origin for o in judge.origins if o.passage_id in by_label}
    return DraftOutcome(
        veracity=assessment.veracity,
        context_need=assessment.context_need,
        context_types=context_types,
        rationale=rationale,
        citations=assessment.citations,
        not_in_sources=not_in_sources,
        review_points=review_points,
        flags=out_flags,
        notes=notes,
        links=links,
        primary_context_type=primary_context,
        checks=checks,
        critic=critic,
        passage_translations=translations,
        origins=origins,
        assessment=assessment,
        versions=vers,
        cost_usd_micros=spent,
        refused=refused,
    )


def _cited_sources(assessment: Assessment, by_label: Mapping[str, PassageData]) -> list[SourceView]:
    sources: dict[str, SourceView] = {}
    for label in assessment.valid_labels:
        p = by_label[label]
        if p.source_url not in sources:
            sources[p.source_url] = SourceView(
                id=f"S{len(sources) + 1}",
                url=p.source_url,
                publisher=p.publisher,
                domain=p.domain,
                tier=p.tier,
                title=p.title,
                published_at=p.published_at,
            )
    return list(sources.values())


def _keep_languages(notes: Sequence[WriterNote], languages: Sequence[str]) -> list[WriterNote]:
    kept: dict[str, WriterNote] = {}
    for note in notes:
        if note.lang in languages and note.lang not in kept:
            kept[note.lang] = note
    return [kept[lang] for lang in languages if lang in kept]


def _run_checks(
    settings: Settings,
    *,
    notes: Sequence[WriterNote],
    links: Sequence[str],
    checks: CheckReport,
    cited: Sequence[PassageData],
    retrieved_urls: Sequence[str],
    post_datetime: datetime | None,
    claim_type: str,
    context_types: Sequence[str],
) -> CheckReport:
    lexicon, allowed = runtime.loaded_terms(settings)
    texts: list[str] = []
    for note in notes:
        texts.extend((note.text, note.fact_sheet))
        if note_length(note.text) > NOTE_MAX_CHARS:
            checks.note_too_long[note.lang] = note_length(note.text)
        seen = {(h.term, h.lang) for h in checks.loaded_terms}
        for hit in find_loaded_terms(
            f"{note.text} {note.fact_sheet}", note.lang, lexicon, allowed=allowed
        ):
            if (hit.term, hit.lang) not in seen:
                seen.add((hit.term, hit.lang))
                checks.loaded_terms.append(hit)
    checks.unknown_links = check_links([*texts, *links], retrieved_urls)
    ongoing = claim_type == ClaimType.ONGOING_SITUATION or ContextType.DEVELOPING in context_types
    dates = check_dates(
        [DatedSource(key=p.label, published_at=p.published_at) for p in cited],
        post_date=post_datetime,
        max_age=_PROVISIONAL_MAX_AGE if ongoing else None,
    )
    checks.sources_after_post = dates["after_post"]
    checks.sources_too_old = dates["too_old"]
    return checks


def note_regime(primary: ContextType | None) -> Regime:
    return CONTEXT_REGIME[primary] if primary is not None else Regime.FORMULATION


# --------------------------------------------------------------------- in-memory run


@dataclass(slots=True)
class InMemoryOutcome:
    veracity: Veracity | None
    context_need: ContextNeed | None
    context_types: list[str]
    confidence: float | None
    band: ConfidenceBand | None
    agreement: Agreement | None
    escalated: bool
    depth: str
    rounds: int
    refused: list[str]
    citations_total: int
    citations_valid: int
    cost_usd_micros: int
    latency_ms: int
    notes: list[dict[str, str]]
    flags: dict[str, Any]
    unverifiable_at_framing: bool


async def run_in_memory(
    llm: LLMClient,
    atlas: AtlasLike,
    claim: ClaimView,
    *,
    level: str,
    post_text: str,
    post_lang: str,
    post_date: date | datetime | None,
    today: date,
    settings: Settings,
    registry_tier: Callable[[str], tuple[str, str | None]] | None = None,
    profile: Profile | None = None,
) -> InMemoryOutcome:
    """E6 to E9 plus the final confidence, without any database write of its own (the
    router still records ``llm_calls`` and budgets). Mirrors the job handlers step by step."""
    started = time.monotonic()
    post_dt: datetime | None
    if isinstance(post_date, datetime):
        post_dt = post_date if post_date.tzinfo else post_date.replace(tzinfo=UTC)
    elif isinstance(post_date, date):
        post_dt = datetime(post_date.year, post_date.month, post_date.day, tzinfo=UTC)
    else:
        post_dt = None
    day = post_dt.date() if post_dt else None
    depth: Depth = initial_depth(level)
    cost = 0
    flags: dict[str, Any] = {"depth": depth.value, "round": 1}
    refused: list[str] = []
    passages: list[PassageData] = []
    queries_seen: list[str] = []
    rounds = 1
    escalated = False
    agreement: Agreement | None = None
    second_pass_flag: bool | None = None
    framing: FrameOutput | None = None
    judge: JudgeOutput | None = None
    synthetic = False
    extra_points: list[str] = []
    unverifiable = False

    def finish_unverifiable(point: str, *, synth_flag: str) -> JudgeOutput:
        flags[synth_flag] = True
        return synthetic_judgement(point)

    try:
        frame = await call_frame(llm, claim, level=level, depth=depth, today=today, post_date=day)
        cost += frame.cost_usd_micros
        framing = frame.output
    except AllRefused as exc:
        capture(ec.FCK_MODEL_REFUSED, exc, role=frame_role(depth))
        refused.append(frame_role(depth))
        judge = finish_unverifiable(REFUSED_FRAME_POINT, synth_flag="unverifiable_at_framing")
        synthetic = unverifiable = True
    else:
        if not framing.verifiable or claim.claim_type in {
            t.value for t in UNVERIFIABLE_CLAIM_TYPES
        }:
            unverifiable = True
            if level == "N4":
                synthetic = True
                judge = finish_unverifiable(
                    framing.verifiability_reason, synth_flag="unverifiable_at_framing"
                )
            else:
                flags["unverifiable_at_framing"] = True
                return InMemoryOutcome(
                    veracity=None,
                    context_need=None,
                    context_types=[],
                    confidence=None,
                    band=None,
                    agreement=None,
                    escalated=False,
                    depth=depth.value,
                    rounds=1,
                    refused=refused,
                    citations_total=0,
                    citations_valid=0,
                    cost_usd_micros=cost,
                    latency_ms=round((time.monotonic() - started) * 1000),
                    notes=[],
                    flags=flags,
                    unverifiable_at_framing=True,
                )

    second: SecondOpinionOutput | None = None
    first_judge: JudgeOutput | None = None
    if framing is not None and judge is None:
        round_ = 1
        while True:
            collected = await collect_round(
                llm,
                atlas,
                settings,
                claim,
                framing,
                level=level,
                depth=depth,
                round_=round_,
                existing=passages,
                previous_queries=queries_seen,
                today=today,
                post_date=day,
                registry_tier=registry_tier,
            )
            cost += collected.cost_usd_micros
            if collected.asymmetric:
                flags["asymmetric_queries"] = True
            queries_seen.extend(
                q.query
                for q in (*collected.queries.output.confirm, *collected.queries.output.refute)
            )
            passages.extend(collected.passages)
            if not passages:
                flags["no_evidence"] = True
                synthetic = True
                judge = synthetic_judgement(
                    needs_more_research=NO_EVIDENCE_POINT,
                )
                break
            second_pass = round_ == 2
            try:
                judged = await call_judge(
                    llm,
                    claim,
                    framing,
                    judge_views(passages, second_pass=second_pass),
                    level=level,
                    depth=depth,
                    today=today,
                    post_date=day,
                )
            except AllRefused as exc:
                capture(ec.FCK_MODEL_REFUSED, exc, role=judge_role(depth))
                refused.append(judge_role(depth))
                synthetic = True
                judge = synthetic_judgement(REFUSED_JUDGE_POINT)
                break
            cost += judged.cost_usd_micros
            if second_pass:
                judge = judged.output
                same = judge.veracity == (first_judge or judge).veracity
                second_pass_flag = same
                flags["second_pass"] = "concordant" if same else "discordant"
                if not same:
                    extra_points.append(
                        "Les deux passes du juge divergent : "
                        f"{(first_judge or judge).veracity.value} puis {judge.veracity.value}."
                    )
                break
            first_judge = judged.output
            try:
                views = judge_views(passages, second_pass=False)
                second_step = await call_second_opinion(
                    llm, claim, framing, views, level=level, today=today, post_date=day
                )
            except AllRefused as exc:
                capture(ec.FCK_MODEL_REFUSED, exc, role=names.ROLE_SECOND_OPINION)
                refused.append(names.ROLE_SECOND_OPINION)
            else:
                cost += second_step.cost_usd_micros
                second = second_step.output
                agreement = compare_opinions(first_judge.veracity, second.veracity)
                flags["second_opinion_veracity"] = second.veracity.value
                if agreement is Agreement.SUBSTANTIVE:
                    try:
                        objection = await call_objection(
                            llm,
                            claim,
                            framing,
                            views,
                            first_judge,
                            second,
                            level=level,
                            today=today,
                            post_date=day,
                        )
                    except AllRefused as exc:
                        capture(ec.FCK_MODEL_REFUSED, exc, role=names.ROLE_OBJECTION)
                        refused.append(names.ROLE_OBJECTION)
                    else:
                        cost += objection.cost_usd_micros
                        o = objection.output
                        flags["objection"] = {
                            "type": o.objection_type.value,
                            "direction": o.direction.value,
                            "explanation": o.explanation,
                            "label": o.passage_id,
                        }
                        extra_points.append(o.explanation)
            provisional = assess_judgement(first_judge, passages)
            confidence, _ = confidence_of(
                settings,
                claim_type=claim.claim_type,
                framing=framing,
                judge=first_judge,
                assessment=provisional,
                agreement=agreement,
                second_pass=None,
            )
            route = decide_route(
                settings,
                profile=profile,
                level=level,
                depth=depth,
                round_=round_,
                agreement=agreement,
                conflicting_sources=first_judge.conflicting_sources,
                confidence=confidence,
                spent_usd_micros=cost,
            )
            if route.cap_reached:
                flags["cap_reached"] = True
            if route.action == "draft":
                judge = first_judge
                break
            rounds = round_ = 2
            flags["round"] = 2
            if route.action == "escalate":
                escalated = True
                depth = Depth.REINFORCED
                flags["escalated"] = True
                flags["depth"] = depth.value
                continue
            # N4: second judge pass without new collection.
            try:
                second_judged = await call_judge(
                    llm,
                    claim,
                    framing,
                    judge_views(passages, second_pass=True),
                    level=level,
                    depth=depth,
                    today=today,
                    post_date=day,
                )
            except AllRefused as exc:
                capture(ec.FCK_MODEL_REFUSED, exc, role=judge_role(depth))
                refused.append(judge_role(depth))
                synthetic = True
                judge = synthetic_judgement(REFUSED_JUDGE_POINT)
                break
            cost += second_judged.cost_usd_micros
            judge = second_judged.output
            same = judge.veracity == first_judge.veracity
            second_pass_flag = same
            flags["second_pass"] = "concordant" if same else "discordant"
            if not same:
                extra_points.append(
                    "Les deux passes du juge divergent : "
                    f"{first_judge.veracity.value} puis {judge.veracity.value}."
                )
            break
    if judge is None:
        raise RuntimeError("in-memory run ended without a judgement")
    if refused:
        flags["refused"] = _unique_refused(refused)
    if agreement is not None:
        flags["agreement"] = agreement.value
    draft = await compose_draft(
        llm,
        settings,
        claim=claim,
        level=level,
        judge=judge,
        synthetic=synthetic,
        passages=passages,
        flags=flags,
        extra_review_points=extra_points,
        post_text=post_text,
        post_lang=post_lang,
        post_datetime=post_dt,
        today=today,
        retrieved_urls=[p.source_url for p in passages],
        last_attempt=False,
        checked_at=datetime.combine(today, dt_time.min, tzinfo=UTC),
    )
    cost += draft.cost_usd_micros
    confidence_value: float | None = None
    band_value: ConfidenceBand | None = None
    if not synthetic:
        confidence_value, band_value = confidence_of(
            settings,
            claim_type=claim.claim_type,
            framing=framing,
            judge=judge,
            assessment=draft.assessment,
            agreement=agreement,
            second_pass=second_pass_flag,
        )
    return InMemoryOutcome(
        veracity=draft.veracity,
        context_need=draft.context_need,
        context_types=draft.context_types,
        confidence=confidence_value,
        band=band_value,
        agreement=agreement,
        escalated=escalated,
        depth=depth.value,
        rounds=rounds,
        refused=_unique_refused([*refused, *draft.refused]),
        citations_total=len(draft.citations),
        citations_valid=sum(1 for c in draft.citations if c.valid),
        cost_usd_micros=cost,
        latency_ms=round((time.monotonic() - started) * 1000),
        notes=[{"lang": n.lang, "text": n.text} for n in draft.notes],
        flags=draft.flags,
        unverifiable_at_framing=unverifiable,
    )
