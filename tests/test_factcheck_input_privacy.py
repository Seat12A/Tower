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
