"""What the models never see: author, handle, display name, metrics, watched status,
processing level. Inputs are built by the real investigate jobs from a claim whose post has
an author, metrics and a watched account; every request is inspected."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain.factcheck.values import Veracity
from tower.core.factcheck import inputs, names, runtime

from .test_investigate_jobs import _request, drain, install, seed
from .test_investigate_pipeline import FakeAtlas, FakeLLM, default_docs, extra_docs, second_opinion

HANDLE = "jane_doe_author"
DISPLAY_NAME = "Jane Doe Authorname"
FORBIDDEN_VALUES = ("987654", "123456", "5555555")


@pytest.fixture(autouse=True)
async def _overrides() -> AsyncIterator[None]:
    yield
    runtime.clear_overrides()


@pytest.mark.parametrize("level", ["N3", "N4"])
async def test_model_inputs_never_carry_author_metrics_or_level(
    db: Database, settings: Settings, level: str
) -> None:
    llm, _ = install(
        FakeLLM({names.ROLE_SECOND_OPINION: second_opinion(Veracity.FALSE)}),
        FakeAtlas([default_docs(), extra_docs()]),
    )
    inv_id = await seed(
        db,
        level=level,
        author_handle=HANDLE,
        author_name=DISPLAY_NAME,
        watched=True,
        post_text=f"Selon @{HANDLE} le chomage a baisse a 7 % en 2025",
    )
    await _request(db, inv_id)
    await drain(db, settings)

    roles = {r.role for r in llm.requests}
    frame = names.ROLE_FRAME_REINFORCED if level == "N4" else names.ROLE_FRAME
    judge = names.ROLE_JUDGE_REINFORCED if level == "N4" else names.ROLE_JUDGE
    assert {
        frame,
        names.ROLE_QUERY_WRITER,
        judge,
        names.ROLE_SECOND_OPINION,
        names.ROLE_OBJECTION,
        names.ROLE_WRITER,
        names.ROLE_CRITIC,
    } <= roles
    for request in llm.requests:
        payload = request.input
        assert inputs.find_forbidden_keys(payload) == [], request.role
        dumped = json.dumps(payload, ensure_ascii=False)
        for secret in (HANDLE, DISPLAY_NAME, *FORBIDDEN_VALUES):
            assert secret not in dumped, (request.role, secret)
        assert level not in payload  # no processing level key at the top either
        assert request.budget_scope == names.level_scope(level)


async def test_forbidden_keys_detector_catches_nested_author() -> None:
    assert inputs.find_forbidden_keys({"claim": {"meta": [{"author": "x"}]}}) == [
        "claim.meta[0].author"
    ]


async def test_pair_arbiter_input_has_only_texts_languages_and_dates(
    db: Database, settings: Settings
) -> None:
    """Real E3 run with an arbiter call: author, handle, reach and level never reach it."""
    from .test_factcheck_matching import Harness, arbiter_out, stat, vec

    h = Harness(db, settings)
    first = "Le taux de chômage est de 7 % en France."
    second = "Le chômage atteint 7 % en France."
    h.embedder.table = {first: vec(1.0), second: vec(0.85)}
    h.llm.arbiter = [arbiter_out(True)]
    await h.post(stat(first), author="987654", metrics={"impressions": 123456})
    await h.post(
        stat(second),
        author="5555555",
        metrics={"impressions": 777},
        post_text=f"{second} cc @{HANDLE}",
    )

    (request,) = h.llm.arbiter_requests
    payload = request.input
    assert set(payload) == {"claim_a", "claim_b"}
    for side in payload.values():
        assert set(side) == {"text", "lang", "post_date"}
    assert inputs.find_forbidden_keys(payload) == []
    dumped = json.dumps(payload, ensure_ascii=False)
    for secret in (HANDLE, *FORBIDDEN_VALUES, "N3", "impressions"):
        assert secret not in dumped
    assert request.role == names.ROLE_PAIR_ARBITER and request.budget_scope == "level:N3"


def test_pair_arbiter_input_builder_rejects_nothing_but_carries_nothing_else() -> None:
    from datetime import date

    payload = inputs.pair_arbiter_input(
        a_text="ˆaˆ", a_lang="fr", a_date=date(2026, 1, 2), b_text="b", b_lang="en", b_date=None
    )
    assert payload == {
        "claim_a": {"text": "ˆaˆ", "lang": "fr", "post_date": "2026-01-02"},
        "claim_b": {"text": "b", "lang": "en", "post_date": None},
    }
    assert inputs.find_forbidden_keys(payload) == []
