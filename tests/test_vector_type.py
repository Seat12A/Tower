"""``halfvec`` column type without the pgvector Python package (round trip, search)."""

from __future__ import annotations

import uuid

from sqlalchemy import select, text

from tower.core.db import Database
from tower.core.db.vector import format_vector, parse_vector
from tower.core.models import Claim, ClaimGroup


def test_format_and_parse() -> None:
    assert parse_vector(format_vector([0.5, -1.0, 2.0])) == [0.5, -1.0, 2.0]
    assert parse_vector(None) is None
    assert parse_vector("[]") == []


def _vec(head: float) -> list[float]:
    return [head] + [0.0] * 510 + [1.0]


async def test_round_trip_and_cosine_search(db: Database) -> None:
    async with db.session() as session, session.begin():
        claim = Claim(
            public_id=uuid.uuid4(),
            lang_original="fr",
            text_original="t",
            text_fr="t",
            claim_type="statistic",
            verifiability=0.9,
            prompt_version="v",
            embedding=_vec(0.5),
            embedding_model="m",
        )
        session.add(claim)
        await session.flush()
        for head in (0.0, 1.0, 10.0):
            session.add(
                ClaimGroup(
                    public_id=uuid.uuid4(),
                    representative_claim_id=claim.id,
                    level="N3",
                    state="queued",
                    representative_embedding=_vec(head),
                )
            )
    async with db.session() as session:
        got = await session.scalar(select(Claim.embedding))
        assert got is not None and len(got) == 512 and got[0] == 0.5
        dist = ClaimGroup.representative_embedding.op("<=>")(_vec(9.0))
        nearest = (
            await session.execute(
                select(ClaimGroup.representative_embedding).order_by(dist).limit(1)
            )
        ).scalar_one()
        assert nearest is not None and nearest[0] == 10.0
        raw = await session.scalar(
            text("SELECT count(*) FROM claim_groups WHERE representative_embedding IS NOT NULL")
        )
        assert raw == 3
