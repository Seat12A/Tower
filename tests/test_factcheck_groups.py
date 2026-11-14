"""Manual merge and split of claim groups, representative recomputation, undo."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain.errors import InvalidInput, NotFound, PermissionDenied
from tower.core.domain.security import Actor
from tower.core.factcheck import names
from tower.core.models.factcheck import Claim, ClaimGroup, ClaimGroupMember, ContentClaim
from tower.core.usecases import groups as groups_uc

from .test_factcheck_matching import (
    Harness,
    _reset,  # noqa: F401  (autouse fixture)
    events_of,
    give_verdict,
    group_rows,
    seed_group,
    vec,
)
from .test_factcheck_service import rows, scalar

ACTOR = Actor.system("test:groups")


@pytest.fixture
def h(db: Database, settings: Settings) -> Harness:
    return Harness(db, settings)


async def public_id(db: Database, group_id: int) -> uuid.UUID:
    return uuid.UUID(
        str(await scalar(db, "SELECT public_id FROM claim_groups WHERE id = :i", i=group_id))
    )


async def add_member(
    db: Database,
    group_id: int,
    text_: str,
    vector: list[float],
    content_id: int | None = None,
) -> uuid.UUID:
    async with db.transaction() as session:
        claim = Claim(
            public_id=uuid.uuid4(),
            lang_original="fr",
            text_original=text_,
            text_fr=text_,
            claim_type="statistic",
            verifiability=0.8,
            prompt_version="pv1",
            embedding=vector,
            embedding_model="scripted-embedding",
        )
        session.add(claim)
        await session.flush()
        session.add(ClaimGroupMember(group_id=group_id, claim_id=claim.id))
        if content_id is not None:
            session.add(ContentClaim(content_id=content_id, claim_id=claim.id, position=1))
        return claim.public_id


async def merge(db: Database, target: int, source: int, *, actor: Actor = ACTOR) -> ClaimGroup:
    t, s = await public_id(db, target), await public_id(db, source)
    async with db.transaction() as session:
        return await groups_uc.merge_groups(
            session, actor, target_id=t, source_id=s, reason="same story"
        )


async def active_members(db: Database, group_id: int) -> list[int]:
    result = await rows(
        db,
        "SELECT claim_id FROM claim_group_members WHERE group_id = :g AND removed_at IS NULL"
        " ORDER BY claim_id",
        g=group_id,
    )
    return [r[0] for r in result]


# ----------------------------------------------------------------------------- merge


async def test_merge_moves_members_and_marks_the_source(db: Database) -> None:
    a = await seed_group(db, "a", vec(1.0))
    b = await seed_group(db, "b", vec(0.9))
    await add_member(db, b, "b2", vec(0.88))
    async with db.transaction() as s:
        await s.execute(
            text("UPDATE claim_groups SET level = 'N4' WHERE id = :i"),
            {"i": b},
        )
    moved_before = await active_members(db, b)

    target = await merge(db, a, b)

    assert target.id == a
    assert await active_members(db, b) == []
    assert set(moved_before) <= set(await active_members(db, a))
    state = {r[0]: r for r in await group_rows(db)}
    assert state[b][1] == "merged" and state[b][5] == a  # merged_into_id
    assert state[a][2] == "N4"  # level = max
    # old rows are kept with a removal date and the reason
    closed = await rows(
        db,
        "SELECT reason FROM claim_group_members WHERE group_id = :g AND removed_at IS NOT NULL",
        g=b,
    )
    assert len(closed) == len(moved_before) and closed[0][0] == "same story"
    for gid in (a, b):
        kinds = [
            r[0]
            for r in await rows(
                db, "SELECT kind FROM claim_group_events WHERE group_id = :g", g=gid
            )
        ]
        assert "merged" in kinds
    merged_event = (await events_of(db, names.CLAIM_GROUP_MERGED))[0]
    assert merged_event["target_group_id"] == str(await public_id(db, a))
    assert merged_event["source_group_id"] == str(await public_id(db, b))
    assert len(merged_event["claim_ids"]) == 2 and merged_event["reason"] == "same story"
    assert merged_event["actor"] == ACTOR.label
    level_events = await events_of(db, names.CLAIM_GROUP_LEVEL_CHANGED)
    assert level_events[0]["to_level"] == "N4"


async def test_merge_recomputes_the_representative_as_the_medoid(db: Database) -> None:
    a = await seed_group(db, "a", vec(1.0))
    b = await seed_group(db, "b", vec(0.0))
    await add_member(db, b, "b2", vec(0.9))
    await merge(db, a, b)
    async with db.session() as session:
        group = await session.get(ClaimGroup, a)
        assert group is not None
        rep = await session.get(Claim, group.representative_claim_id)
        assert rep is not None and rep.text_original == "b2"  # closest to the others
        assert group.representative_embedding is not None
        assert group.representative_embedding[0] == pytest.approx(0.9, abs=1e-2)
    kinds = [
        r[0] for r in await rows(db, "SELECT kind FROM claim_group_events WHERE group_id = :g", g=a)
    ]
    assert "representative_changed" in kinds


async def test_merge_requires_the_permission_and_two_distinct_groups(db: Database) -> None:
    a = await seed_group(db, "a", vec(1.0))
    b = await seed_group(db, "b", vec(0.9))
    with pytest.raises(PermissionDenied):
        await merge(db, a, b, actor=Actor.anonymous())
    pa = await public_id(db, a)
    async with db.transaction() as session:
        with pytest.raises(InvalidInput):
            await groups_uc.merge_groups(session, ACTOR, target_id=pa, source_id=pa, reason="x")
    async with db.transaction() as session:
        with pytest.raises(NotFound) as unknown:
            await groups_uc.merge_groups(
                session, ACTOR, target_id=pa, source_id=uuid.uuid4(), reason="x"
            )
    assert unknown.value.code == "TWR-FCK-020"


async def test_merged_group_cannot_be_merged_again(db: Database) -> None:
    a = await seed_group(db, "a", vec(1.0))
    b = await seed_group(db, "b", vec(0.9))
    c = await seed_group(db, "c", vec(0.8))
    await merge(db, a, b)
    for target, source in ((c, b), (b, c)):
        with pytest.raises(InvalidInput) as err:
            await merge(db, target, source)
        assert err.value.code == "TWR-FCK-021"


async def test_merge_with_a_verdict_only_on_the_source_is_refused(db: Database) -> None:
    a = await seed_group(db, "a", vec(1.0))
    b = await seed_group(db, "b", vec(0.9))
    await give_verdict(db, b, veracity="false")
    with pytest.raises(InvalidInput) as err:
        await merge(db, a, b)
    assert err.value.code == "TWR-FCK-023"
    assert (await group_rows(db))[1][1] != "merged"  # nothing changed


async def test_merge_with_different_veracities_is_refused(db: Database) -> None:
    a = await seed_group(db, "a", vec(1.0))
    b = await seed_group(db, "b", vec(0.9))
    await give_verdict(db, a, veracity="true")
    await give_verdict(db, b, veracity="false")
    with pytest.raises(InvalidInput) as err:
        await merge(db, a, b)
    assert err.value.code == "TWR-FCK-022"


async def test_merge_with_the_same_veracity_supersedes_the_source_verdict(db: Database) -> None:
    a = await seed_group(db, "a", vec(1.0))
    b = await seed_group(db, "b", vec(0.9))
    target_verdict = await give_verdict(db, a, veracity="false")
    source_verdict = await give_verdict(db, b, veracity="false")
    await merge(db, a, b)
    status = {r[0]: r[1] for r in await rows(db, "SELECT id, status FROM verdicts")}
    assert status[source_verdict] == "superseded" and status[target_verdict] == "active"
    state = {r[0]: r for r in await group_rows(db)}
    assert state[a][4] == target_verdict and state[b][4] is None


async def test_merge_with_a_verdict_only_on_the_target_keeps_it(db: Database) -> None:
    a = await seed_group(db, "a", vec(1.0))
    b = await seed_group(db, "b", vec(0.9))
    verdict = await give_verdict(db, a, veracity="false")
    await merge(db, a, b)
    assert (await group_rows(db))[0][4] == verdict


# ---------------------------------------------------------------------------- split


async def split(db: Database, group: int, claims: list[uuid.UUID], **kw: Any) -> ClaimGroup:
    g = await public_id(db, group)
    async with db.transaction() as session:
        return await groups_uc.split_group(
            session, kw.pop("actor", ACTOR), group_id=g, claim_ids=claims, reason="different claim"
        )


async def test_split_moves_claims_to_a_new_group_without_verdict(db: Database, h: Harness) -> None:
    # A real group with a real post (content and investigation) and a verdict.
    from .test_factcheck_matching import stat

    first = stat("Le taux de chômage est de 7 % en France.")
    cid = await h.post(first)
    (group,) = [r[0] for r in await group_rows(db)]
    verdict = await give_verdict(db, group, veracity="false")
    extra = await add_member(db, group, "second member", vec(0.2, axis=5), content_id=cid)
    investigations = await scalar(db, "SELECT count(*) FROM investigations")

    new = await split(db, group, [extra])

    state = {r[0]: r for r in await group_rows(db)}
    assert state[group][4] == verdict  # the source keeps its verdict
    assert state[new.id][1] == "queued" and state[new.id][4] is None
    assert state[new.id][2] == state[group][2]  # same level
    assert await active_members(db, new.id) != [] and len(await active_members(db, group)) == 1
    # the verdict did not propagate: the moved claim is investigated on its own
    assert await scalar(db, "SELECT count(*) FROM investigations") == investigations + 1
    inv = (
        await rows(
            db, "SELECT claim_group_id, content_id FROM investigations ORDER BY id DESC LIMIT 1"
        )
    )[0]
    assert inv[0] == new.id and inv[1] is not None
    event = (await events_of(db, names.CLAIM_GROUP_SPLIT))[0]
    assert event["group_id"] == str(await public_id(db, group))
    assert event["new_group_id"] == str(new.public_id)
    assert event["claim_ids"] == [str(extra)] and event["actor"] == ACTOR.label
    for gid in (group, new.id):
        kinds = [
            r[0]
            for r in await rows(
                db, "SELECT kind FROM claim_group_events WHERE group_id = :g", g=gid
            )
        ]
        assert "split" in kinds


async def test_split_recomputes_both_representatives(db: Database) -> None:
    a = await seed_group(db, "a", vec(1.0))
    b2 = await add_member(db, a, "b2", vec(0.9))
    b3 = await add_member(db, a, "b3", vec(0.88))
    async with db.session() as session:
        rep_before = (await session.get(ClaimGroup, a)).representative_claim_id  # type: ignore[union-attr]
    # move the representative itself plus one more claim
    rep_public = await scalar(db, "SELECT public_id FROM claims WHERE id = :i", i=rep_before)
    new = await split(db, a, [rep_public, b2])
    async with db.session() as session:
        source = await session.get(ClaimGroup, a)
        assert source is not None
        remaining = await active_members(db, a)
        assert remaining == [await scalar(db, "SELECT id FROM claims WHERE public_id = :p", p=b3)]
        assert source.representative_claim_id == remaining[0]
        assert new.representative_claim_id in await active_members(db, new.id)
        assert new.representative_embedding is not None


@pytest.mark.parametrize("case", ["empty", "foreign", "all", "removed"])
async def test_invalid_splits_are_refused(db: Database, case: str) -> None:
    a = await seed_group(db, "a", vec(1.0))
    other = await seed_group(db, "other", vec(0.1, axis=4))
    keep = await add_member(db, a, "keep", vec(0.9))
    moved = await add_member(db, a, "moved", vec(0.8))
    foreign = await scalar(db, "SELECT public_id FROM claims WHERE text_original = 'other'")
    first = await scalar(db, "SELECT public_id FROM claims WHERE text_original = 'a'")
    claims = {
        "empty": [],
        "foreign": [foreign],
        "all": [first, keep, moved],
        "removed": [],
    }[case]
    if case == "removed":
        await split(db, a, [moved])  # now `moved` is no longer an active member of `a`
        claims = [moved]
    with pytest.raises(InvalidInput) as err:
        await split(db, a, claims)
    assert err.value.code == "TWR-FCK-024"
    assert other  # silence the unused seed


async def test_split_of_unverifiable_claims_requests_no_investigation(db: Database) -> None:
    a = await seed_group(db, "a", vec(1.0))
    async with db.transaction() as s:
        member = Claim(
            public_id=uuid.uuid4(),
            lang_original="fr",
            text_original="opinion",
            text_fr="opinion",
            claim_type="opinion",
            verifiability=0.1,
            prompt_version="pv1",
            embedding=vec(0.5),
        )
        s.add(member)
        await s.flush()
        s.add(ClaimGroupMember(group_id=a, claim_id=member.id))
        public = member.public_id
    await split(db, a, [public])
    assert await scalar(db, "SELECT count(*) FROM investigations") == 0


async def test_split_requires_permission_and_a_live_group(db: Database) -> None:
    a = await seed_group(db, "a", vec(1.0))
    b = await seed_group(db, "b", vec(0.9))
    member = await add_member(db, b, "b2", vec(0.8))
    with pytest.raises(PermissionDenied):
        await split(db, b, [member], actor=Actor.anonymous())
    await merge(db, a, b)
    with pytest.raises(InvalidInput) as err:
        await split(db, b, [member])
    assert err.value.code == "TWR-FCK-021"


async def test_merge_then_split_back_undoes_the_merge(db: Database) -> None:
    a = await seed_group(db, "a", vec(1.0))
    b = await seed_group(db, "b", vec(0.3, axis=2))
    extra = await add_member(db, b, "b2", vec(0.35, axis=2))
    original_b = await active_members(db, b)

    await merge(db, a, b)
    journal = (
        await rows(
            db,
            "SELECT claim_ids FROM claim_group_events WHERE group_id = :g AND kind = 'merged'",
            g=a,
        )
    )[0][0]
    assert sorted(journal) == original_b  # the merged event holds the ids to undo
    publics = [
        r[0]
        for r in await rows(
            db, "SELECT public_id FROM claims WHERE id = ANY(:ids) ORDER BY id", ids=journal
        )
    ]
    assert extra in publics

    undone = await split(db, a, publics)
    assert sorted(await active_members(db, undone.id)) == original_b
    assert len(await active_members(db, a)) == 1
    # and the same claims can be merged again (member rows are reopened, not duplicated)
    await merge(db, a, undone.id)
    assert sorted(await active_members(db, a)) == sorted(
        [*original_b, await active_members_first(db, a)]
    )


async def active_members_first(db: Database, group_id: int) -> int:
    """The claim that never moved: the representative of ``a``'s own seed."""
    return int(await scalar(db, "SELECT id FROM claims WHERE text_original = 'a'"))


async def test_recompute_keeps_the_representative_when_nothing_changes(db: Database) -> None:
    a = await seed_group(db, "a", vec(1.0))
    async with db.transaction() as session:
        group = await session.get(ClaimGroup, a)
        assert group is not None
        assert await groups_uc.recompute_representative(session, ACTOR, group) is False


async def test_recompute_ignores_claims_without_vectors(db: Database) -> None:
    a = await seed_group(db, "a", vec(1.0))
    async with db.transaction() as session:
        bare = Claim(
            public_id=uuid.uuid4(),
            lang_original="fr",
            text_original="no vector",
            text_fr="no vector",
            claim_type="statistic",
            verifiability=0.5,
            prompt_version="pv1",
        )
        session.add(bare)
        await session.flush()
        session.add(ClaimGroupMember(group_id=a, claim_id=bare.id))
        group = await session.get(ClaimGroup, a)
        assert group is not None
        assert await groups_uc.recompute_representative(session, ACTOR, group) is False


async def test_join_decision_follows_a_concurrent_merge(db: Database, h: Harness) -> None:
    """The matched group was merged between E3 and the write: the claim joins the live
    group (its representative has the same signature), never the merged one."""
    from tower.core.domain.factcheck.matching import MatchDecision
    from tower.core.models.ingestion import ContentItem
    from tower.core.usecases import claims as claims_uc

    from .test_factcheck_matching import stat
    from .test_factcheck_service import make_content

    text_a = "Le taux de chômage est de 7 % en France."
    text_b = "Selon l'INSEE le chômage vaut 7 % en France."
    h.embedder.table = {text_a: vec(1.0), text_b: vec(0.3, axis=4)}
    await h.post(stat(text_a))
    await h.post(stat(text_b))  # far away: its own group, same signature
    live, old = [r[0] for r in await group_rows(db)]
    await merge(db, live, old)

    text_c = "Le chômage français est de 7 %."
    cid = await make_content(db, "9100", text_c)
    decision = MatchDecision.join(old, "identical_signature:similarity", similarity=0.99)
    async with db.transaction() as session:
        content = await session.get(ContentItem, cid)
        assert content is not None
        record = await claims_uc.record_extraction(
            session,
            ACTOR,
            content=content,
            claims=[stat(text_c)],
            level="N3",
            requested_by="test",
            prompt_version="pv1",
            thread_context=None,
            matches=[decision],
        )
    assert record.group_ids == [live]
    assert len(await active_members(db, live)) == 3
    assert await active_members(db, old) == []


async def test_merge_supersedes_the_drafts_of_the_source_and_review_is_refused(
    db: Database,
) -> None:
    from tower.core.domain.errors import Conflict
    from tower.core.domain.security import Actor, ActorKind, Permission
    from tower.core.usecases.drafts import review_draft

    a = await seed_group(db, "a", vec(1.0))
    b = await seed_group(db, "b", vec(0.9))
    draft = await give_verdict(db, b, status="draft")
    await merge(db, a, b)
    async with db.session() as session:
        status = await session.scalar(
            text("SELECT status FROM verdicts WHERE id = :i"), {"i": draft}
        )
        public = await session.scalar(
            text("SELECT public_id FROM verdicts WHERE id = :i"), {"i": draft}
        )
    assert status == "superseded"

    # A draft still pending on a merged group (e.g. assembled after the merge) is refused.
    async with db.transaction() as session:
        await session.execute(
            text("UPDATE verdicts SET status = 'draft', drafted_at = now() WHERE id = :i"),
            {"i": draft},
        )
    editor = Actor(kind=ActorKind.STAFF, id="1", permissions=frozenset(Permission))
    with pytest.raises(Conflict) as err:
        async with db.transaction() as session:
            await review_draft(session, editor, public, decision="accept")
    assert err.value.code == "TWR-FCK-021"
