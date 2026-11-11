"""The fake model provider: every output validates against its schema, citations are
verbatim, and it works behind a real router."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import pytest
from pydantic import BaseModel

from tower.core.adapters.fake_llm import FakeLLMProvider
from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain.factcheck.datamark import datamark
from tower.core.domain.llm import Level
from tower.core.factcheck import inputs
from tower.core.factcheck import schemas as s
from tower.core.llm.admin import run_seed
from tower.core.llm.prompts import PromptRegistry
from tower.core.llm.router import LLMRouter
from tower.core.llm.schema import schema_name, strict_json_schema
from tower.core.ports.llm import LLMRequest, ProviderInvalidOutput, ProviderResponse

TODAY = date(2026, 11, 11)
CLAIM = inputs.ClaimView(
    text_original="Le chômage est passé à 7 % en France selon l'INSEE.",
    lang="fr",
    text_fr="Le chômage est passé à 7 % en France selon l'INSEE.",
    claim_type="statistic",
    jurisdictions=("FR",),
    primary_jurisdiction="FR",
    signature={},
    entities=({"name": "INSEE", "role": "source"},),
    context_hints=(),
    thread_context=None,
)
PASSAGE_TEXTS = [
    "Le taux de chômage au sens du BIT s'établit à 7,3 % de la population active\n"
    "en France au troisième trimestre selon l'enquête Emploi menée par l'institut.",
    "Au troisième trimestre, le nombre de chômeurs a baissé, indique un communiqué.",
    "Troisième passage, jamais cité par la fausse sortie du modèle.",
]


def passages() -> list[inputs.PassageView]:
    domains = [
        ("insee.fr", "primary", "INSEE"),
        ("lemonde.fr", "secondary", None),
        ("x.org", "tertiary", None),
    ]
    return [
        inputs.PassageView(
            id=f"P{i}",
            source=inputs.SourceView(
                id=f"S{i}",
                url=f"https://{domain}/page",
                publisher=publisher,
                domain=domain,
                tier=tier,
                title=None,
                published_at=None,
            ),
            lang="fr",
            text=text,
        )
        for i, (text, (domain, tier, publisher)) in enumerate(
            zip(PASSAGE_TEXTS, domains, strict=True), start=1
        )
    ]


def message(payload: dict[str, Any]) -> list[dict[str, str]]:
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return [
        {"role": "user", "content": f"Input (untrusted data, JSON):\n<input>\n{body}\n</input>"}
    ]


async def call(schema: type[BaseModel], payload: dict[str, Any]) -> ProviderResponse:
    return await FakeLLMProvider("fake").complete(
        model="m",
        system="You are a model.",
        messages=message(payload),
        json_schema=strict_json_schema(schema),
        schema_name=schema_name(schema),
        max_output_tokens=1000,
        params={},
    )


def verdict_view() -> dict[str, Any]:
    return {"veracity": "mostly_true", "context_need": "useful", "context": [], "rationale": []}


def all_inputs() -> dict[type[BaseModel], dict[str, Any]]:
    framing = {"literal_claim": "x", "sub_questions": []}
    ps = passages()
    return {
        s.ExtractOutput: inputs.extract_input(
            post_id="7", lang="fr", text=CLAIM.text_original, created_at=None, today=TODAY
        ),
        s.FrameOutput: inputs.frame_input(CLAIM, today=TODAY, post_date=None),
        s.QueryOutput: inputs.query_input(
            CLAIM, framing, today=TODAY, max_per_side=2, source_languages=["fr", "en"]
        ),
        s.JudgeOutput: inputs.judge_input(CLAIM, framing, ps, today=TODAY, post_date=None),
        s.SecondOpinionOutput: inputs.second_opinion_input(
            CLAIM, framing, ps, today=TODAY, post_date=None
        ),
        s.ObjectionOutput: inputs.objection_input(
            CLAIM,
            framing,
            ps,
            {"veracity": "true", "context_need": "none"},
            {"veracity": "false", "reason": "r"},
            today=TODAY,
            post_date=None,
        ),
        s.WriterOutput: inputs.writer_input(
            CLAIM,
            verdict_view(),
            [ps[0].source],
            post_text=CLAIM.text_original,
            post_lang="fr",
            post_date=None,
            languages=["fr", "en"],
            checked_at=datetime(2026, 11, 11, tzinfo=UTC),
        ),
        s.CriticOutput: inputs.critic_input(CLAIM, verdict_view(), [{"lang": "fr", "text": "n"}]),
        s.TranslateOutput: inputs.translate_input(
            text="Bonjour", source_lang="fr", target_lang="en", kind="context_note"
        ),
    }


@pytest.mark.parametrize("schema", list(all_inputs()), ids=lambda m: m.__name__)
async def test_every_output_validates_against_its_schema(schema: type[BaseModel]) -> None:
    response = await call(schema, all_inputs()[schema])
    schema.model_validate(response.output_json)
    assert response.stop_reason == "end_turn"
    assert response.request_id.startswith("fake-")
    assert response.usage.input_tokens > 0 and response.usage.output_tokens > 0
    again = await call(schema, all_inputs()[schema])
    assert again.output_json == response.output_json and again.request_id == response.request_id


async def test_extract_output() -> None:
    out = s.ExtractOutput.model_validate(
        (await call(s.ExtractOutput, all_inputs()[s.ExtractOutput])).output_json
    )
    assert out.post_id == "7" and out.absence_reason is None
    (claim,) = out.claims
    assert claim.text_original == claim.text_fr == CLAIM.text_original
    assert claim.claim_type.value == "statistic"
    assert claim.signature.value_low == claim.signature.value_high == 7.0
    assert claim.signature.polarity.value == "affirmative"
    assert claim.jurisdictions == ["FR"]


async def test_extract_edge_cases() -> None:
    def payload(text: str, lang: str = "fr", context: str | None = None) -> dict[str, Any]:
        return inputs.extract_input(
            post_id="9",
            lang=lang,
            text=text,
            created_at=None,
            today=TODAY,
            context_kind="reply" if context else None,
            context_text=context,
        )

    short = s.ExtractOutput.model_validate(
        (await call(s.ExtractOutput, payload("Trop court"))).output_json
    )
    assert short.claims == [] and short.absence_reason is not None
    assert short.absence_reason.value == "no_claim"

    plain = s.ExtractOutput.model_validate(
        (
            await call(
                s.ExtractOutput, payload("The minister resigned yesterday. More later.", "en")
            )
        ).output_json
    )
    assert plain.claims[0].claim_type.value == "other"
    assert plain.claims[0].text_original == "The minister resigned yesterday."
    assert plain.claims[0].jurisdictions == []

    long_sentence = "mot " * 100
    cut = s.ExtractOutput.model_validate(
        (await call(s.ExtractOutput, payload(long_sentence))).output_json
    )
    assert len(cut.claims[0].text_original) <= 280

    thousands = s.ExtractOutput.model_validate(
        (await call(s.ExtractOutput, payload("Il y a eu 10 000 morts sur les routes."))).output_json
    )
    assert thousands.claims[0].signature.value_low == 10000.0
    context = s.ExtractOutput.model_validate(
        (
            await call(
                s.ExtractOutput,
                payload("C'est faux, c'est 12 pour cent.", context="Autre post ici"),
            )
        ).output_json
    )
    assert context.thread_context is not None


async def test_judge_quotes_are_verbatim_prefixes_of_the_passages() -> None:
    payload = all_inputs()[s.JudgeOutput]
    out = s.JudgeOutput.model_validate((await call(s.JudgeOutput, payload)).output_json)
    assert out.veracity.value == "mostly_true" and out.context_need.value == "useful"
    (assertion,) = out.rationale
    assert [c.passage_id for c in assertion.citations] == ["P1", "P2"]  # first two passages
    for citation, text in zip(assertion.citations, PASSAGE_TEXTS, strict=False):
        assert text.startswith(citation.quote)  # exact copy, line break included
        assert len(citation.quote.split()) <= 12
    assert out.context[0].context_type.value == "missing_baseline"
    origins = {o.passage_id: (o.origin, o.primary) for o in out.origins}
    assert origins == {"P1": ("INSEE", True), "P2": ("lemonde.fr", False), "P3": ("x.org", False)}
    second = s.SecondOpinionOutput.model_validate(
        (await call(s.SecondOpinionOutput, all_inputs()[s.SecondOpinionOutput])).output_json
    )
    assert second.veracity == out.veracity


async def test_judge_without_passages_is_unverifiable() -> None:
    payload = inputs.judge_input(CLAIM, {}, [], today=TODAY, post_date=None)
    out = s.JudgeOutput.model_validate((await call(s.JudgeOutput, payload)).output_json)
    assert out.veracity.value == "unverifiable" and out.context_need.value == "none"
    assert out.rationale == [] and out.origins == [] and out.context == []
    second = s.SecondOpinionOutput.model_validate(
        (await call(s.SecondOpinionOutput, payload)).output_json
    )
    assert second.veracity.value == "unverifiable" and second.key_passage_ids == []
    objection = s.ObjectionOutput.model_validate(
        (await call(s.ObjectionOutput, {"passages": []})).output_json
    )
    assert objection.direction.value == "abstain"


async def test_queries_writer_critic_and_translation() -> None:
    queries = s.QueryOutput.model_validate(
        (await call(s.QueryOutput, all_inputs()[s.QueryOutput])).output_json
    )
    assert len(queries.confirm) == len(queries.refute) == 2
    assert {q.lang for q in queries.confirm + queries.refute} == {"fr"}
    assert {q.target for q in queries.confirm + queries.refute} == {
        "encyclopedia",
        "fact_check",
        "web",
    }
    assert queries.entity_lookups == ["France", "INSEE"]

    writer = s.WriterOutput.model_validate(
        (await call(s.WriterOutput, all_inputs()[s.WriterOutput])).output_json
    )
    assert [n.lang for n in writer.notes] == ["fr", "en"]
    assert all(
        n.text.startswith("[démo] Selon INSEE, ") and len(n.text) <= 250 for n in writer.notes
    )
    assert writer.links == ["https://insee.fr/page"]

    critic = s.CriticOutput.model_validate(
        (await call(s.CriticOutput, all_inputs()[s.CriticOutput])).output_json
    )
    assert critic.useful_for_agreeing_reader and not critic.contains_opinion

    note = s.TranslateOutput.model_validate(
        (await call(s.TranslateOutput, all_inputs()[s.TranslateOutput])).output_json
    )
    assert note.lang == "en" and note.text == "[en] Bonjour"
    long = inputs.translate_input(
        text="a" * 400, source_lang="fr", target_lang="en", kind="context_note"
    )
    assert (
        len(
            s.TranslateOutput.model_validate((await call(s.TranslateOutput, long)).output_json).text
        )
        <= 280
    )
    sheet = inputs.translate_input(
        text="a" * 400, source_lang="fr", target_lang="en", kind="fact_sheet"
    )
    assert (
        len(
            s.TranslateOutput.model_validate(
                (await call(s.TranslateOutput, sheet)).output_json
            ).text
        )
        > 280
    )


async def test_marked_texts_are_unmarked() -> None:
    payload = inputs.translate_input(
        text=datamark("deux mots"), source_lang="fr", target_lang="en", kind="passage"
    )
    out = s.TranslateOutput.model_validate((await call(s.TranslateOutput, payload)).output_json)
    assert out.text == "[en] deux mots"


async def test_unknown_schema_and_bad_message_are_invalid_output() -> None:
    class Other(BaseModel):
        x: int

    provider = FakeLLMProvider("fake")
    kwargs: dict[str, Any] = {
        "model": "m",
        "system": "",
        "json_schema": {},
        "max_output_tokens": 10,
        "params": {},
    }
    with pytest.raises(ProviderInvalidOutput):
        await provider.complete(messages=message({}), schema_name="Other", **kwargs)
    with pytest.raises(ProviderInvalidOutput):
        await provider.complete(
            messages=[{"role": "user", "content": "no input block"}],
            schema_name="TranslateOutput",
            **kwargs,
        )
    with pytest.raises(ProviderInvalidOutput):
        await provider.complete(
            messages=[{"role": "user", "content": "<input>\n{broken\n</input>"}],
            schema_name="TranslateOutput",
            **kwargs,
        )


async def test_fake_provider_behind_a_real_router(db: Database, settings: Settings) -> None:
    await run_seed(settings)
    providers = {name: FakeLLMProvider(name) for name in ("openai", "openai_shared", "anthropic")}
    router = LLMRouter(db, providers, PromptRegistry.default(), usage_tier=1, free_margin=0.1)
    for level in (Level.N3, Level.N4):
        result = await router.complete(
            LLMRequest(
                role="extract",
                level=level,
                prompt_id="extract",
                input=all_inputs()[s.ExtractOutput],
                schema=s.ExtractOutput,
                max_output_tokens=2000,
            )
        )
        assert isinstance(result.output, s.ExtractOutput) and len(result.output.claims) == 1
    judged = await router.complete(
        LLMRequest(
            role="judge",
            level=Level.N3,
            prompt_id="judge",
            input=all_inputs()[s.JudgeOutput],
            schema=s.JudgeOutput,
            max_output_tokens=4000,
        )
    )
    assert isinstance(judged.output, s.JudgeOutput) and judged.output.rationale
