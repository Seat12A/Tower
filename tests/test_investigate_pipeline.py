"""Pure steps of the deep investigation (no database): passages, citation checks,
routing, draft composition and the in-memory run. Also hosts the fakes shared with
``test_investigate_jobs.py`` and ``test_factcheck_input_privacy.py``."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

from tower.core.adapters.knowledge.atlas import AtlasDocument, AtlasRequest, AtlasResult
from tower.core.domain.factcheck.values import (
    Agreement,
    ClaimType,
    ContextNeed,
    ContextType,
    Depth,
    ObjectionDirection,
    ObjectionType,
    Veracity,
)
from tower.core.domain.llm import TokenUsage
from tower.core.factcheck import names
from tower.core.factcheck.inputs import ClaimView
from tower.core.factcheck.schemas import (
    CriticOutput,
    FrameOutput,
    JudgeAssertion,
    JudgeCitation,
    JudgeOutput,
    ObjectionOutput,
    PassageOrigin,
    QueryItem,
    QueryOutput,
    SecondOpinionOutput,
    SubQuestion,
    TimeFrame,
    TranslateOutput,
    WriterNote,
    WriterOutput,
)
from tower.core.ports.llm import LLMRequest, LLMResult
from tower.services.investigate import pipeline as pl

from .conftest import make_settings

TODAY = date(2026, 3, 1)
POST_DATE = datetime(2026, 2, 28, 10, 0, tzinfo=UTC)
CLAIM_TEXT = "Unemployment fell to 7 percent in 2025"

# ------------------------------------------------------------------------- fakes


def make_view(**overrides: Any) -> ClaimView:
    base: dict[str, Any] = {
        "text_original": CLAIM_TEXT,
        "lang": "en",
        "text_fr": "Le chômage a baissé à 7 % en 2025",
        "claim_type": ClaimType.STATISTIC.value,
        "jurisdictions": ("FR",),
        "primary_jurisdiction": "FR",
        "signature": {"subject": "unemployment", "predicate": "unemployment_rate"},
        "entities": ({"name": "Insee", "role": "source"},),
        "context_hints": (),
        "thread_context": None,
    }
    base.update(overrides)
    return ClaimView(**base)


def doc(
    url: str,
    domain: str,
    tier: str,
    lang: str,
    text: str,
    *,
    published_at: datetime | None = None,
) -> AtlasDocument:
    return AtlasDocument(
        adapter="fake",
        canonical_url=url,
        original_url=url,
        domain=domain,
        tier=tier,
        lang=lang,
        text=text,
        title=f"Title {domain}",
        publisher=domain,
        published_at=published_at,
    )


def default_docs() -> list[AtlasDocument]:
    return [
        doc(
            "https://insee.fr/chomage",
            "insee.fr",
            "primary",
            "fr",
            "Unemployment rate in France fell to 7 percent in 2025 according to the national "
            "statistics office survey of households. The ILO definition gave a lower rate than "
            "the previous year.",
        ),
        doc(
            "https://lemonde.fr/economie",
            "lemonde.fr",
            "secondary",
            "fr",
            "Newspaper report: unemployment dropped to about 7 percent during 2025, the labour "
            "ministry confirmed on Monday, citing figures published earlier this winter.",
        ),
        doc(
            "https://quelle.de/arbeit",
            "quelle.de",
            "tertiary",
            "de",
            "Die unemployment Quote sank im Jahr 2025 auf sieben percent nach Angaben der "
            "Statistikbehoerde in Paris.",
        ),
        doc(
            "https://uncited.example/blog",
            "uncited.example",
            "tertiary",
            "en",
            "Opinion blog about unemployment and percent figures in 2025 with only commentary "
            "and speculation about the wider economy and politics.",
        ),
    ]


def extra_docs() -> list[AtlasDocument]:
    return [
        doc(
            "https://eurostat.example/series",
            "eurostat.example",
            "primary",
            "en",
            "Eurostat series: harmonised unemployment rate for France stood at 7 percent in 2025, "
            "revised from earlier estimates in the spring release of the statistical office.",
        )
    ]


class FakeAtlas:
    """``collect(AtlasRequest) -> AtlasResult``: one document list per call (the last
    list repeats)."""

    def __init__(self, calls: Sequence[Sequence[AtlasDocument]] | None = None) -> None:
        self.calls = [list(c) for c in (calls if calls is not None else [default_docs()])]
        self.requests: list[AtlasRequest] = []

    async def collect(self, request: AtlasRequest) -> AtlasResult:
        index = min(len(self.requests), len(self.calls) - 1)
        self.requests.append(request)
        docs = self.calls[index] if self.calls else []
        return AtlasResult(
            documents=list(docs),
            web_searches=1,
            paid_cost_usd_micros=0,
            adapters_used=["fake"],
        )


def frame_output(*, verifiable: bool = True, reason: str = "Checkable statistic.") -> FrameOutput:
    return FrameOutput(
        literal_claim=CLAIM_TEXT,
        implied_claims=[],
        charitable_reading=CLAIM_TEXT,
        key_definitions=[],
        time_frame=TimeFrame(
            stated="2025", resolved_start="2025-01-01", resolved_end="2025-12-31", confidence="high"
        ),
        jurisdiction="FR",
        true_if=["The official rate is about 7 percent"],
        false_if=["The official rate is far from 7 percent"],
        known_pitfalls=[],
        sub_questions=[
            SubQuestion(
                question="What was the unemployment rate in 2025?",
                expected_source_type="statistic",
                jurisdiction="FR",
            )
        ],
        verifiable=verifiable,
        verifiability_reason=reason,
        context_hypotheses=[],
    )


def query_output(request: LLMRequest) -> QueryOutput:
    tag = f" round {request.input.get('round', 1)}"
    return QueryOutput(
        confirm=[
            QueryItem(
                query="unemployment rate 2025 France" + tag,
                lang="en",
                source_type="statistic",
                target="web",
            )
        ],
        refute=[
            QueryItem(
                query="unemployment rate 2025 higher than 7 percent" + tag,
                lang="en",
                source_type="statistic",
                target="web",
            )
        ],
        entity_lookups=[],
    )


def _passages(request: LLMRequest) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = request.input["passages"]
    return out


def _words(marked: str, count: int = 6) -> str:
    return " ".join(marked.replace("ˆ", " ").split()[:count])


def judge_output(
    request: LLMRequest,
    veracity: Veracity = Veracity.TRUE,
    *,
    skip_domains: Sequence[str] = ("uncited.example",),
    only_domains: Sequence[str] | None = None,
    fabricate: bool = False,
    context: bool = False,
    conflicting: bool = False,
) -> JudgeOutput:
    cited = [
        p
        for p in _passages(request)
        if p["source"]["domain"] not in skip_domains
        and (only_domains is None or p["source"]["domain"] in only_domains)
    ]
    assertions = [
        JudgeAssertion(
            assertion=f"Le taux est confirmé par {p['source']['domain']}.",
            citations=[JudgeCitation(passage_id=p["id"], quote=_words(p["text"]))],
        )
        for p in cited
    ]
    if fabricate:
        assertions.append(
            JudgeAssertion(
                assertion="Une affirmation inventée.",
                citations=[
                    JudgeCitation(passage_id="P1", quote="words that never appear anywhere")
                ],
            )
        )
    return JudgeOutput(
        veracity=veracity,
        context_need=ContextNeed.NONE,
        context=[],
        rationale=assertions,
        origins=[
            PassageOrigin(
                passage_id=p["id"],
                origin=p["source"]["domain"],
                primary=p["source"]["tier"] == "primary",
            )
            for p in _passages(request)
        ],
        conflicting_sources=conflicting,
        not_in_sources=[],
        review_points=["Vérifier la définition du chômage."],
        needs_more_research=None,
    )


def writer_output(
    request: LLMRequest, text: str = "Le taux officiel est proche de 7 %."
) -> WriterOutput:
    sources = request.input["sources"]
    return WriterOutput(
        notes=[
            WriterNote(lang=lang, text=text, fact_sheet=f"Fact sheet in {lang}.")
            for lang in request.input["languages"]
        ],
        links=[sources[0]["url"]] if sources else [],
        primary_context_type=None,
    )


def critic_output(_: LLMRequest) -> CriticOutput:
    return CriticOutput(
        useful_for_agreeing_reader=True,
        useful_for_disagreeing_reader=True,
        contains_opinion=False,
        contains_generalization=False,
        loaded_words=[],
        unsourced_facts=[],
        judges_author=False,
        comments="RAS",
    )


class FakeLLM:
    """``LLMClient`` double: schema instances chosen per role, requests recorded.

    ``script[role]`` is an instance, an exception (raised), a callable(request) or a list of
    those consumed in order (the last one repeats). Missing roles use sensible defaults."""

    def __init__(self, script: dict[str, Any] | None = None, *, cost: int = 1000) -> None:
        self.script: dict[str, Any] = dict(script or {})
        self.cost = cost
        self.requests: list[LLMRequest] = []
        self.counts: Counter[str] = Counter()

    def calls(self, role: str) -> list[LLMRequest]:
        return [r for r in self.requests if r.role == role]

    async def complete(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        self.counts[request.role] += 1
        entry = self.script.get(request.role)
        if isinstance(entry, list):
            entry = entry.pop(0) if len(entry) > 1 else entry[0]
        if isinstance(entry, BaseException):
            raise entry
        if callable(entry):
            entry = entry(request)
        output = entry if entry is not None else self._default(request)
        return LLMResult(
            output=output,
            provider="fake",
            model="fake-1",
            usage=TokenUsage(),
            cost_usd_micros=self.cost,
            free_tier=False,
            cache_hit=False,
            request_id="req",
            prompt_version="pv1",
            route_version=3,
        )

    def _default(self, request: LLMRequest) -> Any:
        role = request.role
        if role in (names.ROLE_FRAME, names.ROLE_FRAME_REINFORCED):
            return frame_output()
        if role == names.ROLE_QUERY_WRITER:
            return query_output(request)
        if role in (names.ROLE_JUDGE, names.ROLE_JUDGE_REINFORCED):
            return judge_output(request)
        if role == names.ROLE_SECOND_OPINION:
            return SecondOpinionOutput(
                veracity=Veracity.TRUE,
                context_need=ContextNeed.NONE,
                key_passage_ids=["P1"],
                reason="Same reading.",
            )
        if role == names.ROLE_OBJECTION:
            return ObjectionOutput(
                objection_type=ObjectionType.INSUFFICIENT_EVIDENCE,
                passage_id="P1",
                direction=ObjectionDirection.LESS_SEVERE,
                explanation="Les preuves sont partielles.",
            )
        if role == names.ROLE_WRITER:
            return writer_output(request)
        if role == names.ROLE_CRITIC:
            return critic_output(request)
        if role == names.ROLE_TRANSLATE:
            return TranslateOutput(lang="fr", text="Texte traduit.")
        raise AssertionError(f"unexpected role {role}")


def second_opinion(veracity: Veracity) -> SecondOpinionOutput:
    return SecondOpinionOutput(
        veracity=veracity,
        context_need=ContextNeed.NONE,
        key_passage_ids=["P1"],
        reason="Independent reading.",
    )


# -------------------------------------------------------------------- passages


def _pd(label: str, text: str, *, source: str | None = None, **kw: Any) -> pl.PassageData:
    base: dict[str, Any] = {
        "label": label,
        "text": text,
        "lang": "fr",
        "source_url": source or f"https://{label.lower()}.example/x",
        "domain": f"{label.lower()}.example",
        "tier": "secondary",
        "publisher": None,
        "title": None,
        "published_at": None,
        "simhash": hash(label) & 0xFFFFFFFF,
        "score": 1.0,
        "round": 1,
    }
    base.update(kw)
    return pl.PassageData(**base)


def test_judge_views_orders_by_score_then_reverses_for_second_pass() -> None:
    passages = [
        _pd("P1", "one", score=1.0),
        _pd("P2", "two", score=3.0),
        _pd("P3", "three", score=2.0),
        _pd("P4", "four", score=0.5, round=2),
    ]
    assert [v.id for v in pl.judge_views(passages, second_pass=False)] == ["P2", "P3", "P1"]
    assert [v.id for v in pl.judge_views(passages, second_pass=True)] == ["P4", "P3", "P2", "P1"]


def test_passage_views_number_sources_by_first_appearance() -> None:
    passages = [
        _pd("P1", "a", source="https://s1.example/a"),
        _pd("P2", "b", source="https://s2.example/b"),
        _pd("P3", "c", source="https://s1.example/a"),
    ]
    views = pl.passage_views(passages)
    assert [views[label].source.id for label in ("P1", "P2", "P3")] == ["S1", "S2", "S1"]


def test_select_new_passages_round_two_skips_duplicates_and_respects_limit() -> None:
    settings = make_settings(fc_passages_reinforced=4, fc_passages_standard=4)
    framing = frame_output()
    queries = query_output(LLMRequest("q", None, "q", {}, QueryOutput, 1))  # type: ignore[arg-type]
    docs = default_docs()
    first = pl.select_new_passages(
        make_view(),
        framing,
        queries,
        docs,
        [],
        settings=settings,
        round_=1,
        reinforced=False,
    )
    assert [p.label for p in first] == ["P1", "P2", "P3", "P4"]
    again = pl.select_new_passages(
        make_view(),
        framing,
        queries,
        [*docs, *extra_docs()],
        first[:2],
        settings=settings,
        round_=2,
        reinforced=True,
    )
    assert [p.label for p in again] == ["P3", "P4"]
    assert not {p.source_url for p in again[:0]}
    assert all(p.round == 2 for p in again)
    # Nothing the first two passages already contain is added again.
    assert all(p.text not in {f.text for f in first[:2]} for p in again)


def test_atlas_request_search_budget() -> None:
    settings = make_settings(fc_searches_standard=4, fc_searches_reinforced_extra=3)
    queries = query_output(LLMRequest("q", None, "q", {}, QueryOutput, 1))  # type: ignore[arg-type]
    view = make_view()

    def searches(depth: Depth, round_: int) -> int:
        return pl.atlas_request(
            view, queries, settings=settings, depth=depth, round_=round_, as_of=None
        ).max_web_searches

    assert searches(Depth.STANDARD, 1) == 4
    assert searches(Depth.REINFORCED, 1) == 7
    assert searches(Depth.REINFORCED, 2) == 3
    request = pl.atlas_request(
        view, queries, settings=settings, depth=Depth.STANDARD, round_=1, as_of=None
    )
    assert {q.side for q in request.queries} == {"confirm", "refute"}


# ------------------------------------------------------------- citation checks


def _judge(
    rationale: list[JudgeAssertion],
    origins: list[PassageOrigin],
    veracity: Veracity = Veracity.TRUE,
) -> JudgeOutput:
    return JudgeOutput(
        veracity=veracity,
        context_need=ContextNeed.NONE,
        context=[],
        rationale=rationale,
        origins=origins,
        conflicting_sources=False,
        not_in_sources=[],
        review_points=[],
        needs_more_research=None,
    )


def _cite(label: str, quote: str, text: str = "x") -> JudgeAssertion:
    return JudgeAssertion(
        assertion=f"A {label}", citations=[JudgeCitation(passage_id=label, quote=quote)]
    )


def test_assess_valid_citations_independence_and_primary() -> None:
    passages = [
        _pd("P1", "The rate was seven percent in 2025.", tier="primary", simhash=0),
        _pd("P2", "Seven percent unemployment, said the ministry.", simhash=-1),
    ]
    judge = _judge(
        [_cite("P1", "rate was seven percent"), _cite("P2", "Seven percent unemployment")],
        [
            PassageOrigin(passage_id="P1", origin="Insee", primary=True),
            PassageOrigin(passage_id="P2", origin="Ministry", primary=False),
            PassageOrigin(passage_id="P9", origin="Ghost", primary=True),
        ],
    )
    a = pl.assess_judgement(judge, passages)
    assert a.valid_labels == ["P1", "P2"]
    assert a.veracity is Veracity.TRUE and not a.downgraded
    assert a.independence.independent_origins == 2 and a.independence.has_primary
    assert a.primary_labels == ["P1"]  # the unknown P9 is ignored


def test_assess_invalid_and_unknown_citations_and_downgrade() -> None:
    passages = [_pd("P1", "The rate was seven percent in 2025.", tier="primary")]
    judge = _judge(
        [_cite("P1", "rate was seven percent"), _cite("P1", "never written"), _cite("P7", "x")],
        [PassageOrigin(passage_id="P1", origin="Insee", primary=True)],
    )
    a = pl.assess_judgement(judge, passages)
    assert [c.valid for c in a.citations] == [True, False, False]
    assert a.valid_labels == ["P1"]
    # One independent origin only: TRUE becomes UNVERIFIABLE, context at least useful.
    assert a.veracity is Veracity.UNVERIFIABLE and a.downgraded
    assert a.context_need is ContextNeed.USEFUL


def test_assess_accepts_datamarked_quote_and_ellipsis() -> None:
    passages = [_pd("P1", "Alpha beta gamma. Delta epsilon zeta eta theta.")]
    judge = _judge(
        [_cite("P1", "Alphaˆbeta"), _cite("P1", "Alpha ... theta")],
        [],
        Veracity.MISLEADING,
    )
    assert [c.valid for c in pl.assess_judgement(judge, passages).citations] == [True, True]


# ----------------------------------------------------------------------- routing


def _route(
    settings: Any = None,
    *,
    level: str = "N3",
    depth: Depth = Depth.STANDARD,
    round_: int = 1,
    agreement: Agreement | None = Agreement.IDENTICAL,
    conflicting: bool = False,
    confidence: float = 0.9,
    spent: int = 1000,
) -> pl.Route:
    return pl.decide_route(
        settings or make_settings(),
        level=level,
        depth=depth,
        round_=round_,
        agreement=agreement,
        conflicting_sources=conflicting,
        confidence=confidence,
        spent_usd_micros=spent,
    )


def test_decide_route_standard() -> None:
    assert _route().action == "draft"
    assert _route(agreement=Agreement.SUBSTANTIVE).action == "escalate"
    assert _route(conflicting=True).action == "escalate"
    assert _route(confidence=0.3).action == "escalate"
    # Cap of the standard depth reached: no escalation, flagged.
    capped = _route(agreement=Agreement.SUBSTANTIVE, spent=100_000)
    assert capped.action == "draft" and capped.cap_reached
    # A cap reached without any reason to escalate is not worth a flag.
    assert _route(spent=500_000) == pl.Route("draft", False)


def test_decide_route_reinforced() -> None:
    n4: dict[str, Any] = {"level": "N4", "depth": Depth.REINFORCED}
    assert _route(**n4).action == "second_pass"
    assert _route(**n4, agreement=None).action == "second_pass"
    capped = _route(**n4, spent=600_000)
    assert capped.action == "draft" and capped.cap_reached
    assert _route(**n4, round_=2).action == "draft"
    # An escalated N3 (reinforced, round 2) goes to the draft.
    assert _route(depth=Depth.REINFORCED, round_=2).action == "draft"


def test_cap_micros_and_languages() -> None:
    settings = make_settings()
    assert pl.cap_micros(settings, "N3", Depth.STANDARD) == 100_000
    assert pl.cap_micros(settings, "N3", Depth.REINFORCED) == 400_000
    assert pl.cap_micros(settings, "N4", Depth.REINFORCED) == 600_000
    assert pl.writer_languages(settings, "fr") == ["fr"]
    assert pl.writer_languages(settings, "en") == ["en", "fr"]
    assert pl.writer_languages(settings, "de") == ["de", "fr"]


# ------------------------------------------------------------------ draft compose


async def _compose(
    llm: FakeLLM, passages: list[pl.PassageData], judge: JudgeOutput, **kw: Any
) -> pl.DraftOutcome:
    params: dict[str, Any] = {
        "claim": make_view(),
        "level": "N3",
        "judge": judge,
        "synthetic": False,
        "passages": passages,
        "flags": {},
        "extra_review_points": [],
        "post_text": "Some post",
        "post_lang": "fr",
        "post_datetime": POST_DATE,
        "today": TODAY,
        "retrieved_urls": [p.source_url for p in passages],
        "last_attempt": False,
    }
    params.update(kw)
    return await pl.compose_draft(llm, make_settings(), **params)


def _two_sources() -> tuple[list[pl.PassageData], JudgeOutput]:
    passages = [
        _pd("P1", "The rate was seven percent in 2025.", tier="primary", simhash=0),
        _pd("P2", "Seven percent unemployment, said the ministry.", simhash=-1),
    ]
    judge = _judge(
        [_cite("P1", "rate was seven percent"), _cite("P2", "Seven percent unemployment")],
        [
            PassageOrigin(passage_id="P1", origin="Insee", primary=True),
            PassageOrigin(passage_id="P2", origin="Ministry", primary=False),
        ],
    )
    return passages, judge


async def test_compose_draft_drops_assertions_without_valid_citation() -> None:
    passages, judge = _two_sources()
    judge = judge.model_copy(
        update={"rationale": [*judge.rationale, _cite("P1", "words nobody wrote")]}
    )
    llm = FakeLLM()
    out = await _compose(llm, passages, judge)
    assert [r["assertion"] for r in out.rationale] == ["A P1", "A P2"]
    assert out.checks.invalid_quotes == ["P1"]
    assert sum(1 for c in out.citations if not c.valid) == 1
    assert out.flags["downgraded"] is False
    assert out.flags["primary_passages"] == ["P1"]
    assert out.origins == {"P1": "Insee", "P2": "Ministry"}
    assert [n.lang for n in out.notes] == ["fr"]
    assert out.critic is not None and out.flags["critic"]["comments"] == "RAS"
    # The writer sees cited sources renumbered S1.. and only valid quotes.
    writer_input = llm.calls(names.ROLE_WRITER)[0].input
    assert [s["id"] for s in writer_input["sources"]] == ["S1", "S2"]
    assert len(writer_input["verdict"]["rationale"]) == 2
    assert out.versions["prompt"]["writer"] == "pv1" and out.versions["route"]["critic"] == 3


async def test_compose_draft_checks_links_length_and_lexicon() -> None:
    passages, judge = _two_sources()
    long_text = "Mot " * 80 + "scandale https://elsewhere.example/page"
    llm = FakeLLM(
        {
            names.ROLE_WRITER: lambda r: WriterOutput(
                notes=[WriterNote(lang="fr", text=long_text, fact_sheet="Fiche.")],
                links=["https://unknown.example/z", passages[0].source_url],
                primary_context_type=ContextType.DEVELOPING,
            )
        }
    )
    out = await _compose(llm, passages, judge)
    assert out.links == [passages[0].source_url]
    assert "https://elsewhere.example/page" in out.checks.unknown_links
    assert "https://unknown.example/z" in out.checks.unknown_links
    assert out.checks.note_too_long["fr"] > 280
    assert out.checks.blocking
    assert out.primary_context_type is ContextType.DEVELOPING


async def test_compose_draft_synthetic_skips_the_note() -> None:
    llm = FakeLLM()
    judge = pl.synthetic_judgement("point")
    out = await _compose(llm, [], judge, synthetic=True)
    assert out.notes == [] and out.veracity is Veracity.UNVERIFIABLE
    assert out.review_points == ["point"]
    assert not llm.requests


async def test_compose_draft_translates_cited_foreign_passages() -> None:
    passages = [
        _pd("P1", "Die Quote war sieben percent im Jahr.", lang="de", tier="primary", simhash=0),
        _pd("P2", "Seven percent unemployment, said the ministry.", simhash=-1),
    ]
    judge = _judge(
        [_cite("P1", "Quote war sieben percent"), _cite("P2", "Seven percent unemployment")],
        [
            PassageOrigin(passage_id="P1", origin="Amt", primary=True),
            PassageOrigin(passage_id="P2", origin="Ministry", primary=False),
        ],
    )
    llm = FakeLLM()
    out = await _compose(llm, passages, judge)
    assert out.passage_translations == {"P1": "Texte traduit."}
    assert llm.counts[names.ROLE_TRANSLATE] == 1


# --------------------------------------------------------------- in-memory run


async def _run(
    llm: FakeLLM,
    atlas: FakeAtlas | None = None,
    *,
    level: str = "N3",
    settings: Any = None,
    view: ClaimView | None = None,
) -> pl.InMemoryOutcome:
    return await pl.run_in_memory(
        llm,
        atlas or FakeAtlas(),
        view or make_view(),
        level=level,
        post_text="Some post",
        post_lang="fr",
        post_date=POST_DATE,
        today=TODAY,
        settings=settings or make_settings(),
    )


async def test_run_in_memory_happy_path() -> None:
    llm = FakeLLM()
    out = await _run(llm)
    assert out.veracity is Veracity.TRUE
    assert out.agreement is Agreement.IDENTICAL
    assert not out.escalated and out.rounds == 1 and out.depth == "standard"
    assert out.citations_total == out.citations_valid == 3
    assert out.confidence is not None and out.confidence >= 0.8
    assert out.band is not None and out.band.value == "high"
    assert out.notes and out.notes[0]["lang"] == "fr"
    # frame, queries, judge, second opinion, writer, critic, one German translation.
    assert out.cost_usd_micros == 7000
    assert llm.counts[names.ROLE_JUDGE] == 1
    assert out.unverifiable_at_framing is False and out.latency_ms >= 0


async def test_run_in_memory_substantive_disagreement_escalates() -> None:
    llm = FakeLLM({names.ROLE_SECOND_OPINION: second_opinion(Veracity.FALSE)})
    atlas = FakeAtlas([default_docs(), extra_docs()])
    out = await _run(llm, atlas)
    assert out.escalated and out.depth == "reinforced" and out.rounds == 2
    assert out.agreement is Agreement.SUBSTANTIVE
    assert len(atlas.requests) == 2
    assert atlas.requests[1].max_web_searches == make_settings().fc_searches_reinforced_extra
    assert llm.counts[names.ROLE_JUDGE_REINFORCED] == 1 and llm.counts[names.ROLE_JUDGE] == 1
    assert llm.counts[names.ROLE_OBJECTION] == 1
    assert out.flags["objection"]["label"] == "P1"
    assert out.flags["second_pass"] == "concordant"
    # Round 2 queries differ from round 1 (the cache key must change).
    first, second = llm.calls(names.ROLE_QUERY_WRITER)
    assert first.input != second.input and second.input["round"] == 2
    # Second pass reads every passage in reversed order.
    ids = [p["id"] for p in llm.calls(names.ROLE_JUDGE_REINFORCED)[0].input["passages"]]
    assert ids == sorted(ids, reverse=True) and len(ids) == 5


async def test_run_in_memory_n4_second_pass_without_new_collection() -> None:
    llm = FakeLLM(
        {
            names.ROLE_JUDGE_REINFORCED: [
                lambda r: judge_output(r, Veracity.TRUE),
                lambda r: judge_output(r, Veracity.MOSTLY_TRUE),
            ]
        }
    )
    atlas = FakeAtlas()
    out = await _run(llm, atlas, level="N4")
    assert len(atlas.requests) == 1
    assert (
        llm.counts[names.ROLE_JUDGE_REINFORCED] == 2
        and llm.counts[names.ROLE_FRAME_REINFORCED] == 1
    )
    assert out.depth == "reinforced" and out.rounds == 2 and not out.escalated
    assert out.veracity is Veracity.MOSTLY_TRUE
    assert out.flags["second_pass"] == "discordant"
    assert atlas.requests[0].max_web_searches == 8


async def test_run_in_memory_unverifiable_at_framing() -> None:
    llm = FakeLLM({names.ROLE_FRAME: frame_output(verifiable=False, reason="Opinion.")})
    n3 = await _run(llm)
    assert n3.unverifiable_at_framing and n3.veracity is None and n3.notes == []
    assert llm.counts[names.ROLE_QUERY_WRITER] == 0
    llm4 = FakeLLM({names.ROLE_FRAME_REINFORCED: frame_output(verifiable=False, reason="Opinion.")})
    n4 = await _run(llm4, level="N4")
    assert n4.unverifiable_at_framing and n4.veracity is Veracity.UNVERIFIABLE
    assert n4.context_need is ContextNeed.NONE and n4.notes == []
    # A prediction is never investigated, whatever the model says.
    pred = await _run(FakeLLM(), view=make_view(claim_type=ClaimType.PREDICTION.value))
    assert pred.unverifiable_at_framing


async def test_run_in_memory_no_evidence_skips_judge() -> None:
    llm = FakeLLM()
    out = await _run(llm, FakeAtlas([[]]))
    assert out.veracity is Veracity.UNVERIFIABLE and out.flags["no_evidence"]
    assert llm.counts[names.ROLE_JUDGE] == 0 and out.confidence is None


async def test_run_in_memory_refusals_never_become_a_verdict() -> None:
    from tower.core.llm.errors import AllRefused

    out = await _run(FakeLLM({names.ROLE_JUDGE: AllRefused("refused")}))
    assert out.veracity is Veracity.UNVERIFIABLE and out.refused == [names.ROLE_JUDGE]
    out = await _run(FakeLLM({names.ROLE_FRAME: AllRefused("refused")}))
    assert out.unverifiable_at_framing and out.veracity is Veracity.UNVERIFIABLE
    out = await _run(FakeLLM({names.ROLE_SECOND_OPINION: AllRefused("refused")}))
    assert out.agreement is None and out.veracity is Veracity.TRUE
    out = await _run(FakeLLM({names.ROLE_WRITER: AllRefused("refused")}))
    assert out.notes == [] and names.ROLE_WRITER in out.refused


async def test_run_in_memory_downgrades_without_independent_primary() -> None:
    llm = FakeLLM(
        {names.ROLE_JUDGE: lambda r: judge_output(r, Veracity.TRUE, only_domains=["lemonde.fr"])}
    )
    out = await _run(llm)
    assert out.veracity is Veracity.UNVERIFIABLE and out.flags["downgraded"] is True
    assert out.context_need is ContextNeed.USEFUL
