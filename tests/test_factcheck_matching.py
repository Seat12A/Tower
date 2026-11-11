"""E3 matching: pgvector candidate search, the JOIN/NEW decision with real rows, arbiter
branches and trap pairs, end to end through ``extract`` and ``record_extraction``.

Real PostgreSQL. Embeddings are scripted (exact cosines), the model is a role-aware script.
"""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from tower.core.adapters.fake_llm import FakeEmbedder
from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.db.vector import format_vector
from tower.core.domain.factcheck.matching import Candidate
from tower.core.domain.factcheck.signatures import compare_signatures
from tower.core.domain.factcheck.values import ArbiterReason, ClaimType
from tower.core.domain.llm import TokenUsage
from tower.core.factcheck import names, runtime
from tower.core.factcheck.candidates import (
    NearRow,
    build_candidates,
    claim_signature,
    nearest_groups,
    signature_from_extracted,
)
from tower.core.factcheck.profiles import invalidate_profile_cache
from tower.core.factcheck.schemas import ExtractedClaim, PairArbiterOutput
from tower.core.jobs.queue import RetryLater
from tower.core.llm.embeddings import EmbedResult
from tower.core.llm.errors import AllRefused, BudgetExhausted
from tower.core.models.factcheck import Claim, ClaimGroup, ClaimGroupMember
from tower.core.ports.llm import LLMRequest, LLMResult

from .test_factcheck_service import (
    claim,
    ctx,
    make_content,
    output,
    rows,
    run_extract,
    scalar,
    signature,
)

SAME = 0.92
GREY = 0.80
UNEMPLOYMENT = "Le taux de chômage est de 7 % en France."
UNEMPLOYMENT_BIS = "Le chômage atteint 7 % en France."


# ------------------------------------------------------------------------ test doubles


def vec(cos: float, axis: int = 1) -> list[float]:
    """Unit vector whose cosine with ``vec(1.0)`` is ``cos``."""
    out = [0.0] * 512
    out[0] = cos
    out[axis] = math.sqrt(max(0.0, 1.0 - cos * cos))
    return out


class ScriptedEmbedder:
    """Text → vector from a table (exact cosines), else the deterministic fake."""

    model_name = "scripted-embedding"

    def __init__(self, table: Mapping[str, list[float]] | None = None) -> None:
        self.table: dict[str, list[float]] = dict(table or {})
        self.calls: list[list[str]] = []
        self._fake = FakeEmbedder()

    async def embed(self, texts: Sequence[str], *, level: str, ref: str) -> EmbedResult:
        self.calls.append(list(texts))
        fallback = await self._fake.embed(list(texts), dimensions=512)
        vectors = [self.table.get(t, f) for t, f in zip(texts, fallback, strict=True)]
        return EmbedResult(vectors, self.model_name, 0, 0)


def arbiter_out(same: bool, reason: ArbiterReason = ArbiterReason.SAME_CLAIM) -> PairArbiterOutput:
    return PairArbiterOutput(same=same, reason=reason, explanation="Decisive element.")


class RoleLLM:
    """``LLMClient`` answering by role: the extraction output, a queue of arbiter answers
    (an exception raises; an empty queue answers "different")."""

    def __init__(self, *arbiter: Any) -> None:
        self.extract_output: Any = None
        self.arbiter = list(arbiter)
        self.requests: list[LLMRequest] = []

    @property
    def arbiter_requests(self) -> list[LLMRequest]:
        return [r for r in self.requests if r.role == names.ROLE_PAIR_ARBITER]

    async def complete(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        if request.role == names.ROLE_EXTRACT:
            item = self.extract_output
        else:
            item = self.arbiter.pop(0) if self.arbiter else arbiter_out(False)
        if isinstance(item, Exception):
            raise item
        return LLMResult(
            output=item,
            provider="fake",
            model="fake",
            usage=TokenUsage(),
            cost_usd_micros=0,
            free_tier=False,
            cache_hit=False,
            request_id="r",
            prompt_version="pv1",
            route_version=1,
        )


class Harness:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.embedder = ScriptedEmbedder()
        self.llm = RoleLLM()
        self._n = 0
        runtime.set_override("embedder", self.embedder)
        runtime.set_override("llm", self.llm)

    async def post(
        self,
        claims: ExtractedClaim | Sequence[ExtractedClaim],
        *,
        lang: str = "fr",
        level: str = "N3",
        post_text: str | None = None,
        author: str | None = "5000",
        created: datetime | None = None,
        metrics: dict[str, int] | None = None,
    ) -> int:
        """Ingest a post whose text is the claim text, extract it, return the content id."""
        items = [claims] if isinstance(claims, ExtractedClaim) else list(claims)
        self._n += 1
        body = post_text or " ".join(c.text_original for c in items)
        cid = await make_content(
            self.db,
            str(7000 + self._n),
            body,
            lang=lang,
            author=author,
            created=created,
            metrics=metrics,
        )
        self.llm.extract_output = output(cid, items)
        await run_extract(self.db, self.settings, cid, level)
        return cid


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    runtime.clear_overrides()
    invalidate_profile_cache()
    yield
    runtime.clear_overrides()
    invalidate_profile_cache()


@pytest.fixture
def h(db: Database, settings: Settings) -> Harness:
    return Harness(db, settings)


def stat(text_: str, value: float = 7.0, **sig: Any) -> ExtractedClaim:
    """A French statistic claim ("7 %") with a consistent signature."""
    base: dict[str, Any] = {
        "subject": "taux de chômage",
        "predicate": "unemployment_rate",
        "value_low": value - 0.5,
        "value_high": value + 0.5,
        "unit": "%",
    }
    base.update(sig)
    return claim(text_, sig=signature(**base))


async def group_rows(db: Database) -> list[Any]:
    return await rows(
        db,
        "SELECT id, state, level, representative_claim_id, active_verdict_id, merged_into_id"
        " FROM claim_groups ORDER BY id",
    )


async def events_of(db: Database, topic: str) -> list[dict[str, Any]]:
    result = await rows(db, "SELECT payload FROM events WHERE topic = :t ORDER BY id", t=topic)
    return [r[0] for r in result]


async def seed_group(
    db: Database,
    text_: str,
    vector: list[float],
    *,
    lang: str = "fr",
    state: str = "queued",
    merged_into: int | None = None,
    sig: Mapping[str, Any] | None = None,
) -> int:
    """A claim and its group with the given (pivot = original) vector."""
    values = {
        "subject": "taux de chômage",
        "value_low": 6.5,
        "value_high": 7.5,
        "unit": "%",
        **(sig or {}),
    }
    async with db.transaction() as session:
        row = Claim(
            public_id=uuid.uuid4(),
            lang_original=lang,
            text_original=text_,
            text_fr=text_,
            claim_type="statistic",
            subject=values["subject"],
            value_low=values["value_low"],
            value_high=values["value_high"],
            unit=values["unit"],
            primary_jurisdiction="FR",
            verifiability=0.8,
            prompt_version="pv1",
            embedding=vector,
            embedding_model="scripted-embedding",
        )
        session.add(row)
        await session.flush()
        group = ClaimGroup(
            public_id=uuid.uuid4(),
            representative_claim_id=row.id,
            level="N3",
            state=state,
            representative_embedding=vector,
            merged_into_id=merged_into,
        )
        session.add(group)
        await session.flush()
        session.add(ClaimGroupMember(group_id=group.id, claim_id=row.id))
        return int(group.id)


# ----------------------------------------------------------------- pgvector search


async def test_nearest_groups_orders_by_cosine_and_skips_merged(db: Database) -> None:
    near = await seed_group(db, "near", vec(0.95))
    far = await seed_group(db, "far", vec(0.30))
    mid = await seed_group(db, "mid", vec(0.70))
    merged = await seed_group(db, "merged", vec(0.99), state="merged", merged_into=near)
    async with db.session() as session:
        result = await nearest_groups(session, vec(1.0), limit=10)
    assert [r.group_id for r in result] == [near, mid, far]
    assert merged not in {r.group_id for r in result}
    assert result[0].distance == pytest.approx(1 - 0.95, abs=1e-3)
    assert result[0].claim_lang == "fr" and result[0].original_embedding is not None
    async with db.session() as session:
        top2 = await nearest_groups(session, vec(1.0), limit=2)
    assert [r.group_id for r in top2] == [near, mid]


async def test_nearest_groups_ignores_groups_without_vector(db: Database) -> None:
    await seed_group(db, "with vector", vec(0.9))
    async with db.transaction() as session:
        await session.execute(text("UPDATE claim_groups SET representative_embedding = NULL"))
    async with db.session() as session:
        assert await nearest_groups(session, vec(1.0), limit=5) == []


async def test_hnsw_index_serves_the_search(db: Database) -> None:
    async with db.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO claims (public_id, lang_original, text_original, text_fr,"
                " claim_type, verifiability, prompt_version)"
                " SELECT gen_random_uuid(), 'fr', 'c' || g, 'c' || g, 'statistic', 0.5, 'pv1'"
                " FROM generate_series(1, 300) g"
            )
        )
        ids = list(await session.scalars(text("SELECT id FROM claims ORDER BY id")))
        for i, claim_id in enumerate(ids):
            v = [0.0] * 512
            v[i % 512] = 1.0
            await session.execute(
                text(
                    "INSERT INTO claim_groups (public_id, representative_claim_id, level, state,"
                    " representative_embedding) VALUES (gen_random_uuid(), :c, 'N3', 'queued',"
                    " CAST(CAST(:v AS TEXT) AS halfvec(512)))"
                ),
                {"c": claim_id, "v": format_vector(v)},
            )
        await session.execute(text("ANALYZE claim_groups"))
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(
            str(r[0])
            for r in await session.execute(
                text(
                    "EXPLAIN SELECT id FROM claim_groups WHERE representative_embedding IS NOT NULL"
                    " AND state <> 'merged' ORDER BY representative_embedding <=>"
                    " CAST(CAST(:q AS TEXT) AS halfvec(512)) LIMIT 5"
                ),
                {"q": format_vector(vec(1.0))},
            )
        )
    assert "ix_claim_groups_representative_embedding" in plan


async def test_build_candidates_similarities_and_signature_comparison() -> None:
    now = datetime.now(UTC)
    a = stat(UNEMPLOYMENT)
    sig = signature_from_extracted(a, now.date())

    def row(lang: str, original: list[float] | None, distance: float) -> NearRow:
        return NearRow(
            group_id=1 if lang == "fr" else 2,
            public_id=uuid.uuid4(),
            distance=distance,
            level="N3",
            claim_id=1,
            claim_lang=lang,
            claim_text="x",
            signature=sig,
            original_embedding=original,
            post_date=now.date(),
            active_verdict_id=7,
            verdict_status="active",
            verdict_expires_at=None,
        )

    cands = build_candidates(
        [row("fr", vec(0.9), 0.05), row("en", vec(0.9), 0.04)],
        claim_lang="fr",
        signature=sig,
        original_embedding=vec(1.0),
        current_period_days=31,
    )
    fr, en = cands
    assert fr.similarity_pivot == pytest.approx(0.95)
    assert fr.similarity_original == pytest.approx(0.9)
    assert en.similarity_original is None  # interlingual: pivot only
    assert fr.comparison.identical and fr.has_active_verdict
    assert isinstance(fr, Candidate)


async def test_claim_signature_matches_extracted_signature() -> None:
    day = datetime.now(UTC).date()
    extracted = stat(UNEMPLOYMENT)
    row = Claim(
        lang_original="fr",
        text_original=extracted.text_original,
        text_fr=extracted.text_fr,
        claim_type="statistic",
        subject="taux de chômage",
        predicate="unemployment_rate",
        value_low=6.5,
        value_high=7.5,
        unit="%",
        primary_jurisdiction="FR",
        polarity="affirmative",
        verifiability=0.8,
        prompt_version="pv1",
    )
    cmp = compare_signatures(
        signature_from_extracted(extracted, day), claim_signature(row, day), current_period_days=31
    )
    assert cmp.identical


# ----------------------------------------------------------- JOIN / NEW decisions


async def test_identical_signature_and_high_similarity_joins_without_arbiter(
    db: Database, h: Harness
) -> None:
    h.embedder.table = {UNEMPLOYMENT: vec(1.0), UNEMPLOYMENT_BIS: vec(0.95)}
    await h.post(stat(UNEMPLOYMENT))
    await h.post(stat(UNEMPLOYMENT_BIS))

    assert h.llm.arbiter_requests == []
    groups = await group_rows(db)
    assert len(groups) == 1
    assert (
        await scalar(db, "SELECT count(*) FROM claim_group_members WHERE removed_at IS NULL") == 2
    )
    joined = await rows(db, "SELECT data FROM claim_group_events WHERE kind = 'joined'")
    assert len(joined) == 1
    assert joined[0][0]["rule"] == "identical_signature:similarity"
    assert joined[0][0]["similarity"] == pytest.approx(0.95, abs=1e-3)
    created = await events_of(db, names.CLAIM_GROUP_CREATED)
    assert len(created) == 1 and created[0]["rule"] == "new:no_candidate"
    assert created[0]["related_group_id"] is None
    # claim.extracted carries the final group ids of the second post
    extracted = await events_of(db, names.CLAIM_EXTRACTED)
    assert extracted[1]["group_ids"] == extracted[0]["group_ids"]
    # one investigation only: the second claim is covered by the group
    assert await scalar(db, "SELECT count(*) FROM investigations") == 1


async def test_grey_zone_arbiter_says_same_joins(db: Database, h: Harness) -> None:
    h.embedder.table = {UNEMPLOYMENT: vec(1.0), UNEMPLOYMENT_BIS: vec(0.85)}
    h.llm.arbiter = [arbiter_out(True)]
    await h.post(stat(UNEMPLOYMENT))
    await h.post(stat(UNEMPLOYMENT_BIS))

    assert len(h.llm.arbiter_requests) == 1
    assert len(await group_rows(db)) == 1
    data = (await rows(db, "SELECT data FROM claim_group_events WHERE kind = 'joined'"))[0][0]
    assert data["rule"] == "identical_signature:arbiter"
    assert data["arbiter_reason"] == "same_claim"


async def test_grey_zone_arbiter_says_different_creates_a_group(db: Database, h: Harness) -> None:
    h.embedder.table = {UNEMPLOYMENT: vec(1.0), UNEMPLOYMENT_BIS: vec(0.85)}
    h.llm.arbiter = [arbiter_out(False, ArbiterReason.DIFFERENT_SUBJECT)]
    await h.post(stat(UNEMPLOYMENT))
    await h.post(stat(UNEMPLOYMENT_BIS))

    assert len(h.llm.arbiter_requests) == 1
    assert len(await group_rows(db)) == 2
    created = await events_of(db, names.CLAIM_GROUP_CREATED)
    assert created[1]["rule"] == "new:arbiter_different"


async def test_arbiter_same_with_another_reason_is_read_as_different(
    db: Database, h: Harness
) -> None:
    h.embedder.table = {UNEMPLOYMENT: vec(1.0), UNEMPLOYMENT_BIS: vec(0.85)}
    h.llm.arbiter = [arbiter_out(True, ArbiterReason.DIFFERENT_VALUE)]
    await h.post(stat(UNEMPLOYMENT))
    await h.post(stat(UNEMPLOYMENT_BIS))
    assert len(await group_rows(db)) == 2


async def test_below_grey_threshold_never_calls_the_arbiter(db: Database, h: Harness) -> None:
    h.embedder.table = {UNEMPLOYMENT: vec(1.0), UNEMPLOYMENT_BIS: vec(0.5)}
    await h.post(stat(UNEMPLOYMENT))
    await h.post(stat(UNEMPLOYMENT_BIS))
    assert h.llm.arbiter_requests == []
    assert len(await group_rows(db)) == 2
    created = await events_of(db, names.CLAIM_GROUP_CREATED)
    assert created[1]["rule"] == "new:below_grey"


async def test_interlingual_pair_always_goes_to_the_arbiter(db: Database, h: Harness) -> None:
    english = "The unemployment rate is 7% in France."
    h.embedder.table = {UNEMPLOYMENT: vec(1.0), english: vec(0.4, axis=7)}
    # the English claim's French pivot text is the very text of the French group
    h.llm.arbiter = [arbiter_out(True)]
    await h.post(stat(UNEMPLOYMENT))
    en_claim = stat(english).model_copy(update={"text_fr": UNEMPLOYMENT})
    await h.post(en_claim, lang="en")

    requests = h.llm.arbiter_requests
    assert len(requests) == 1  # pivot similarity is 1.0 and still needs the arbiter
    payload = requests[0].input
    assert payload["claim_a"]["lang"] == "en" and payload["claim_b"]["lang"] == "fr"
    assert "ˆ" in payload["claim_a"]["text"]  # datamarked originals, not the translation
    assert len(await group_rows(db)) == 1
    data = (await rows(db, "SELECT data FROM claim_group_events WHERE kind = 'joined'"))[0][0]
    assert data["rule"] == "identical_signature:arbiter"
    # one embedding call for the English claim: original + French pivot text
    assert h.embedder.calls[-1] == [english] or h.embedder.calls[-1] == [english, UNEMPLOYMENT]


async def test_non_french_claim_embeds_original_and_pivot_in_one_call(
    db: Database, h: Harness
) -> None:
    english = "Unemployment stands at 7% in France."
    french = "Le chômage est à 7 % en France."
    await h.post(stat(english).model_copy(update={"text_fr": french}), lang="en")
    assert h.embedder.calls == [[english, french]]
    row = (
        await rows(
            db,
            "SELECT embedding IS NOT NULL, embedding_pivot IS NOT NULL,"
            " embedding_model FROM claims",
        )
    )[0]
    assert tuple(row) == (True, True, "scripted-embedding")
    # French claims store no pivot vector (the original is the pivot)
    await h.post(stat(UNEMPLOYMENT))
    fr_row = (await rows(db, "SELECT embedding_pivot IS NULL FROM claims ORDER BY id DESC"))[0]
    assert fr_row[0] is True


async def test_stored_vectors_are_reused_instead_of_embedded_again(
    db: Database, h: Harness
) -> None:
    await h.post(stat(UNEMPLOYMENT))
    assert h.embedder.calls == [[UNEMPLOYMENT]]
    await h.post(stat(UNEMPLOYMENT))
    assert h.embedder.calls == [[UNEMPLOYMENT]]  # second post: no new provider call


async def test_missing_only_difference_creates_a_related_group(db: Database, h: Harness) -> None:
    h.embedder.table = {UNEMPLOYMENT: vec(1.0), UNEMPLOYMENT_BIS: vec(0.97)}
    h.llm.arbiter = [arbiter_out(True)]
    first = stat(UNEMPLOYMENT, definition_variant=None)
    second = stat(UNEMPLOYMENT_BIS, definition_variant="ilo")  # set on one side only: MISSING
    await h.post(first)
    await h.post(second)

    assert len(h.llm.arbiter_requests) == 1
    groups = await group_rows(db)
    assert len(groups) == 2  # never joined: the verdict would not cover the variant
    created = await events_of(db, names.CLAIM_GROUP_CREATED)
    assert created[1]["rule"] == "new:related"
    assert created[1]["related_group_id"] is not None
    journal = (
        await rows(
            db,
            "SELECT other_group_id, data FROM claim_group_events"
            " WHERE kind = 'created' ORDER BY id",
        )
    )[1]
    assert journal[0] == groups[0][0] and journal[1]["rule"] == "new:related"


async def test_missing_only_difference_with_arbiter_different_has_no_related(
    db: Database, h: Harness
) -> None:
    h.embedder.table = {UNEMPLOYMENT: vec(1.0), UNEMPLOYMENT_BIS: vec(0.97)}
    h.llm.arbiter = [arbiter_out(False, ArbiterReason.DIFFERENT_DEFINITION)]
    await h.post(stat(UNEMPLOYMENT))
    await h.post(stat(UNEMPLOYMENT_BIS, definition_variant="ilo"))
    created = await events_of(db, names.CLAIM_GROUP_CREATED)
    assert created[1]["related_group_id"] is None


TRAPS: list[tuple[str, dict[str, Any], str]] = [
    ("another value", {"value_low": 8.5, "value_high": 9.5}, "Le chômage atteint 9 % en France."),
    ("negation", {"polarity": "negative"}, "Le chômage n'est pas de 7 % en France."),
    ("period", {"period_start": "2019-01-01", "period_end": "2019-12-31"}, UNEMPLOYMENT_BIS),
    ("territory", {"place": "DE"}, "Le taux de chômage est de 7 % en Allemagne."),
    ("unit", {"unit": "persons"}, UNEMPLOYMENT_BIS),
]


@pytest.mark.parametrize(("label", "sig", "second_text"), TRAPS, ids=[t[0] for t in TRAPS])
async def test_trap_pairs_are_never_joined_nor_reused(
    db: Database, h: Harness, label: str, sig: dict[str, Any], second_text: str
) -> None:
    # Same vector (cosine 1.0) and a verdict on the first group: only the signature protects.
    h.embedder.table = {UNEMPLOYMENT: vec(1.0), second_text: vec(1.0)}
    await h.post(stat(UNEMPLOYMENT, period_start="2024-01-01", period_end="2024-12-31"))
    await give_verdict(db)
    second = stat(second_text, **{"period_start": "2024-01-01", "period_end": "2024-12-31", **sig})
    await h.post(second)

    assert len(await group_rows(db)) == 2, label
    assert await events_of(db, names.VERDICT_REUSED) == [], label
    assert h.llm.arbiter_requests == [], label  # conflicting signature: not even asked
    assert await scalar(db, "SELECT count(*) FROM investigations") == 2, label


async def test_arbiter_failure_means_different_and_is_captured(
    db: Database, h: Harness, reporter: Any
) -> None:
    h.embedder.table = {UNEMPLOYMENT: vec(1.0), UNEMPLOYMENT_BIS: vec(0.85)}
    h.llm.arbiter = [AllRefused("refused")]
    await h.post(stat(UNEMPLOYMENT))
    await h.post(stat(UNEMPLOYMENT_BIS))
    assert len(await group_rows(db)) == 2
    await reporter.flush()
    assert (await scalar(db, "SELECT count(*) FROM error_reports WHERE code = 'TWR-FCK-034'")) >= 1


async def test_arbiter_budget_exhausted_retries_the_job_later(db: Database, h: Harness) -> None:
    h.embedder.table = {UNEMPLOYMENT: vec(1.0), UNEMPLOYMENT_BIS: vec(0.85)}
    await h.post(stat(UNEMPLOYMENT))
    h.llm.arbiter = [BudgetExhausted("level:N3", datetime.now(UTC) + timedelta(hours=1))]
    cid = await make_content(db, "9001", UNEMPLOYMENT_BIS)
    h.llm.extract_output = output(cid, [stat(UNEMPLOYMENT_BIS)])
    with pytest.raises(RetryLater):
        await run_extract(db, h.settings, cid)
    assert await scalar(db, "SELECT count(*) FROM claims") == 1  # nothing was written


async def test_authors_and_reach_never_change_the_decision(db: Database, h: Harness) -> None:
    h.embedder.table = {UNEMPLOYMENT: vec(1.0), UNEMPLOYMENT_BIS: vec(0.95)}
    await h.post(stat(UNEMPLOYMENT), author="1111", metrics={"impressions": 5})
    await h.post(stat(UNEMPLOYMENT_BIS), author="2222", metrics={"impressions": 9_000_000})
    assert len(await group_rows(db)) == 1


async def test_two_identical_claims_of_one_post_share_a_group(db: Database, h: Harness) -> None:
    first = stat("Le chômage est de 7 % en France.")
    second = stat("Le taux de chômage français vaut 7 %.")
    h.embedder.table = {first.text_original: vec(1.0), second.text_original: vec(0.1, axis=3)}
    await h.post([first, second])
    assert len(await group_rows(db)) == 1
    assert (
        await scalar(db, "SELECT count(*) FROM claim_group_members WHERE removed_at IS NULL") == 2
    )
    assert await scalar(db, "SELECT count(*) FROM investigations") == 1
    extracted = (await events_of(db, names.CLAIM_EXTRACTED))[0]
    assert extracted["group_ids"][0] == extracted["group_ids"][1]
    journal = await rows(db, "SELECT data FROM claim_group_events WHERE kind = 'joined'")
    assert journal[0][0]["rule"] == "same_post:identical_signature"


async def test_two_claims_of_one_post_without_value_stay_apart(db: Database, h: Harness) -> None:
    a = claim("Le gouvernement a augmenté les impôts.", claim_type=ClaimType.OTHER)
    b = claim("Le gouvernement a supprimé des postes.", claim_type=ClaimType.OTHER)
    await h.post([a, b])
    assert len(await group_rows(db)) == 2


async def test_stale_join_decision_becomes_a_new_group(db: Database, h: Harness) -> None:
    """A concurrent change made the target's representative differ: never join blindly."""
    from tower.core.domain.factcheck.matching import MatchDecision
    from tower.core.domain.security import Actor
    from tower.core.models.ingestion import ContentItem
    from tower.core.usecases import claims as claims_uc

    await h.post(stat(UNEMPLOYMENT))
    (target,) = [r[0] for r in await group_rows(db)]
    cid = await make_content(db, "9002", "Le chômage est de 9 % en France.")
    other = stat("Le chômage est de 9 % en France.", value=9.0)
    decision = MatchDecision.join(target, "identical_signature:similarity", similarity=0.99)
    async with db.transaction() as session:
        content = await session.get(ContentItem, cid)
        assert content is not None
        record = await claims_uc.record_extraction(
            session,
            Actor.system("test"),
            content=content,
            claims=[other],
            level="N3",
            requested_by="test",
            prompt_version="pv1",
            thread_context=None,
            matches=[decision],
        )
    assert record.group_ids[0] != target
    assert len(await group_rows(db)) == 2


async def test_matches_must_align_with_claims(db: Database, h: Harness) -> None:
    from tower.core.domain.security import Actor
    from tower.core.models.ingestion import ContentItem
    from tower.core.usecases import claims as claims_uc

    cid = await make_content(db, "9003", UNEMPLOYMENT)
    async with db.transaction() as session:
        content = await session.get(ContentItem, cid)
        assert content is not None
        with pytest.raises(ValueError, match="one decision per claim"):
            await claims_uc.record_extraction(
                session,
                Actor.system("test"),
                content=content,
                claims=[stat(UNEMPLOYMENT)],
                level="N3",
                requested_by="t",
                prompt_version="pv1",
                thread_context=None,
                matches=[],
            )


async def test_without_matches_every_claim_gets_its_own_group(db: Database, h: Harness) -> None:
    """Lot 3 behaviour is kept for callers that do not match."""
    from tower.core.domain.security import Actor
    from tower.core.models.ingestion import ContentItem
    from tower.core.usecases import claims as claims_uc

    for ext in ("9004", "9005"):
        cid = await make_content(db, ext, UNEMPLOYMENT)
        async with db.transaction() as session:
            content = await session.get(ContentItem, cid)
            assert content is not None
            await claims_uc.record_extraction(
                session,
                Actor.system("test"),
                content=content,
                claims=[stat(UNEMPLOYMENT)],
                level="N3",
                requested_by="t",
                prompt_version="pv1",
                thread_context=None,
            )
    assert len(await group_rows(db)) == 2
    assert await events_of(db, names.CLAIM_GROUP_CREATED) == []


# ------------------------------------------------------------------------- helpers


async def give_verdict(
    db: Database,
    group_id: int | None = None,
    *,
    veracity: str = "false",
    expires_at: datetime | None = None,
    status: str = "active",
) -> int:
    """Attach a verdict to ``group_id`` (default: the only group) and make it active."""
    from tower.core.models.factcheck import Verdict

    async with db.transaction() as session:
        gid = group_id or int(
            await session.scalar(text("SELECT id FROM claim_groups LIMIT 1")) or 0
        )
        verdict = Verdict(
            public_id=uuid.uuid4(),
            claim_group_id=gid,
            veracity=veracity,
            context_need="none",
            status=status,
            validated_at=datetime.now(UTC),
            validated_by="staff:1",
            expires_at=expires_at,
        )
        session.add(verdict)
        await session.flush()
        if status == "active":
            await session.execute(
                text(
                    "UPDATE claim_groups SET active_verdict_id = :v, state = 'verified'"
                    " WHERE id = :g"
                ),
                {"v": verdict.id, "g": gid},
            )
        return int(verdict.id)


__all__ = ["ctx", "give_verdict"]


# ------------------------------------------------------------------ fake provider


async def _fake_arbiter(a: str, b: str) -> PairArbiterOutput:
    from tower.core.adapters.fake_llm import FakeLLMProvider
    from tower.core.domain.factcheck.datamark import datamark

    payload = {
        "claim_a": {"text": datamark(a), "lang": "fr", "post_date": None},
        "claim_b": {"text": datamark(b), "lang": "fr", "post_date": None},
    }
    response = await FakeLLMProvider("fake").complete(
        model="m",
        system="s",
        messages=[{"role": "user", "content": "<input>\n" + json.dumps(payload) + "\n</input>"}],
        json_schema={},
        schema_name="PairArbiterOutput",
        max_output_tokens=100,
        params={},
    )
    return PairArbiterOutput.model_validate(response.output_json)


@pytest.mark.parametrize(
    ("a", "b", "same", "reason"),
    [
        (
            "Le chômage est de 7 % en France.",
            "Le chômage atteint 7 % en France.",
            True,
            "same_claim",
        ),
        ("Le chômage est de 7 %.", "Le chômage est de 9 %.", False, "different_value"),
        (
            "Le chômage est de 7 % en 2023.",
            "Le chômage est de 7 % en 2019.",
            False,
            "different_period",
        ),
        ("Le chômage est de 7 %.", "Le chômage n'est pas de 7 %.", False, "opposite_polarity"),
        ("It is not 7%.", "It is never 7%.", True, "same_claim"),
    ],
)
async def test_fake_pair_arbiter_is_deterministic(a: str, b: str, same: bool, reason: str) -> None:
    out = await _fake_arbiter(a, b)
    assert (out.same, out.reason.value) == (same, reason)


async def test_fake_extract_honours_the_french_marker() -> None:
    from tower.core.adapters.fake_llm import FakeLLMProvider
    from tower.core.domain.factcheck.datamark import datamark

    post = "Unemployment is 7% in France. [fr: Le chômage est de 7 % en France.]"
    payload = {"post": {"id": "1", "lang": "en", "text": datamark(post), "context": None}}
    response = await FakeLLMProvider("fake").complete(
        model="m",
        system="s",
        messages=[{"role": "user", "content": "<input>\n" + json.dumps(payload) + "\n</input>"}],
        json_schema={},
        schema_name="ExtractOutput",
        max_output_tokens=100,
        params={},
    )
    first = response.output_json["claims"][0]
    assert first["text_original"] == "Unemployment is 7% in France."
    assert first["text_fr"] == "Le chômage est de 7 % en France."
