"""Narratives: attachment end to end, maintenance, titles, merge and split (real DB)."""

from __future__ import annotations

import itertools
import json
import os
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain import error_codes as ec
from tower.core.domain.errors import InvalidInput, NotFound, PermissionDenied
from tower.core.domain.ingestion import AuthorInput, ItemInput
from tower.core.domain.llm import TokenUsage
from tower.core.domain.security import Actor
from tower.core.factcheck import names, runtime
from tower.core.factcheck.schemas import NarrativeTitleOutput
from tower.core.llm.errors import AllRefused
from tower.core.models.factcheck import ClaimGroup
from tower.core.models.factcheck_v2 import Narrative
from tower.core.models.ingestion import ContentMedia, MediaAsset
from tower.core.ports.llm import LLMRequest, LLMResult
from tower.core.usecases import ingestion as ing
from tower.core.usecases import narratives as nar
from tower.services.factcheck.narratives import (
    attach_narrative,
    maintain_narratives,
    narrative_title,
    on_group_created,
)

from .conftest import make_settings
from .test_factcheck_matching import (
    _reset,  # noqa: F401  (autouse fixture)
    events_of,
    seed_group,
    vec,
)
from .test_factcheck_service import ctx, event, jobs_of, rows, scalar

SYSTEM = Actor.system("test:narratives")
_ids = itertools.count(910_000)


def unit(axis: int) -> list[float]:
    """Unit vector on its own axis: cosine 0 with every other ``unit``."""
    out = [0.0] * 512
    out[axis] = 1.0
    return out


async def seed(
    db: Database,
    label: str,
    vector: list[float],
    *,
    entities: tuple[tuple[str, str | None], ...] = (),
    reach: int = 0,
    level: str = "N1",
    theme: str | None = None,
) -> int:
    gid = await seed_group(db, label, vector, state="dormant")
    payload = [{"name": n, "role": "subject", "qid": q} for n, q in entities]
    async with db.transaction() as s:
        await s.execute(
            text(
                "UPDATE claims SET entities = CAST(:e AS jsonb), theme = :t "
                "WHERE id = (SELECT representative_claim_id FROM claim_groups WHERE id = :g)"
            ),
            {"e": json.dumps(payload), "t": theme, "g": gid},
        )
        await s.execute(
            text("UPDATE claim_groups SET reach = :r, level = :l WHERE id = :g"),
            {"r": reach, "l": level, "g": gid},
        )
    return gid


async def seed_many(db: Database, count: int, axis: int, *, reach: int = 0) -> list[int]:
    """``count`` groups on orthogonal axes; the reach of group ``i`` is ``i * reach``."""
    return [await seed(db, f"g{axis}-{i}", unit(axis + i), reach=i * reach) for i in range(count)]


async def link(
    db: Database,
    gid: int,
    texts: list[str],
    *,
    platform: str = "x",
    now: datetime | None = None,
    metrics: dict[str, int] | None = None,
    authors: list[str] | None = None,
) -> list[int]:
    """Ingest one post per text (distinct author each) and link it to the group's claim."""
    contents: list[int] = []
    for i, body in enumerate(texts):
        n = next(_ids)
        ext = str(n) if platform == "x" else f"p{n}"
        author = authors[i] if authors else str(700_000 + n)
        async with db.transaction() as session:
            await ing.ingest_items(
                session,
                SYSTEM,
                [
                    ItemInput(
                        platform=platform,
                        external_id=ext,
                        text=f"{body} #{n}",
                        author=AuthorInput(external_id=author, handle=f"u{n}"),
                        lang_hint="fr",
                        created_at=now or datetime.now(UTC) - timedelta(minutes=5),
                        metrics=metrics or {},
                    )
                ],
                source="radar",
                filter_version=None,
                installation_token=None,
                max_items=10,
                daily_quota=10_000,
                now=now,
            )
        contents.append(
            int(
                await scalar(
                    db,
                    "SELECT id FROM content_items WHERE platform = :p AND external_id = :e",
                    p=platform,
                    e=ext,
                )
            )
        )
    for content_id in contents:
        await link_content(db, content_id, gid)
    return contents


async def link_content(db: Database, content_id: int, gid: int) -> None:
    async with db.transaction() as s:
        await s.execute(
            text(
                "INSERT INTO content_claims (content_id, claim_id, position) "
                "SELECT :c, representative_claim_id, 1 FROM claim_groups WHERE id = :g "
                "ON CONFLICT DO NOTHING"
            ),
            {"c": content_id, "g": gid},
        )


async def add_media(db: Database, content_ids: list[int], media_id: int | None = None) -> int:
    async with db.transaction() as s:
        if media_id is None:
            media = MediaAsset(
                kind="image", url="https://img.example/a.png", url_sha256=os.urandom(32)
            )
            s.add(media)
            await s.flush()
            media_id = media.id
        for content_id in content_ids:
            s.add(ContentMedia(content_id=content_id, media_id=media_id, position=0))
    return media_id


async def public_id(db: Database, gid: int) -> uuid.UUID:
    return uuid.UUID(
        str(await scalar(db, "SELECT public_id FROM claim_groups WHERE id = :i", i=gid))
    )


async def attach(db: Database, settings: Settings, gid: int) -> int:
    """Run the attach job; return the id of the narrative the group ends up in."""
    payload = {"group_id": str(await public_id(db, gid))}
    await attach_narrative(ctx(db, settings, names.JOB_ATTACH_NARRATIVE, payload))
    return int(
        await scalar(
            db,
            "SELECT narrative_id FROM narrative_groups WHERE group_id = :g AND removed_at IS NULL",
            g=gid,
        )
    )


async def narrative_of(db: Database, gid: int) -> int:
    return int(
        await scalar(
            db,
            "SELECT narrative_id FROM narrative_groups WHERE group_id = :g AND removed_at IS NULL",
            g=gid,
        )
    )


async def kinds(db: Database, narrative_id: int) -> list[str]:
    result = await rows(
        db, "SELECT kind FROM narrative_events WHERE narrative_id = :n ORDER BY id", n=narrative_id
    )
    return [r[0] for r in result]


async def build_narrative(db: Database, *gids: int) -> int:
    """A narrative founded by the first group, the others attached by hand."""
    async with db.transaction() as s:
        groups = [await s.get(ClaimGroup, g) for g in gids]
        assert all(g is not None for g in groups)
        created = await nar.create_narrative(s, SYSTEM, group=groups[0])  # type: ignore[arg-type]
        for g in groups[1:]:
            await nar.attach_group(s, SYSTEM, created, g, signals={})  # type: ignore[arg-type]
        return created.id


@pytest.fixture
def cfg() -> Settings:
    return make_settings()


LINK_A = "https://news.example/story"
LINK_A_ALT = "https://www.news.example/story?utm=1"
ACME = (("Acme Corp", "Q1"),)


# ------------------------------------------------------------------------- attach


async def test_first_group_founds_a_narrative_and_counts_its_entities(
    db: Database, cfg: Settings
) -> None:
    a = await seed(db, "a", vec(1.0), entities=ACME, theme="economy")
    nid = await attach(db, cfg, a)
    row = (
        await rows(
            db,
            "SELECT state, founder_group_id, medoid_group_ids, theme, title_status, "
            "previous_narrative_id, medoids_elected_at IS NOT NULL FROM narratives WHERE id = :n",
            n=nid,
        )
    )[0]
    assert row[0] == "active" and row[1] == a and list(row[2]) == [a]
    assert row[3] == "economy" and row[4] == "none" and row[5] is None and row[6]
    assert await kinds(db, nid) == ["created"]
    assert await scalar(db, "SELECT count FROM entity_rarity WHERE entity_key = 'Q1'") == 1


async def test_a_similar_group_with_a_shared_entity_and_link_attaches(
    db: Database, cfg: Settings
) -> None:
    a = await seed(db, "a", vec(1.0), entities=ACME)
    await link(db, a, [f"Acme claims it {LINK_A}"])
    first = await attach(db, cfg, a)
    b = await seed(db, "b", vec(0.9), entities=ACME)
    await link(db, b, [f"More on Acme {LINK_A_ALT}"])

    second = await attach(db, cfg, b)

    assert second == first
    members = await rows(
        db,
        "SELECT group_id, reason, signals FROM narrative_groups WHERE narrative_id = :n "
        "ORDER BY added_at",
        n=first,
    )
    assert [m[0] for m in members] == [a, b] and members[1][1] == "match"
    detail = members[1][2]
    assert detail["signals"]["semantic"] and detail["signals"]["entity"]
    assert not detail["signals"]["media"]
    assert detail["anchors"]["rare_entity"] and detail["anchors"]["shared_link"]
    assert detail["medoid_similarity"] == pytest.approx(0.9, abs=0.01)
    assert await kinds(db, first) == ["created", "attached"]
    assert await scalar(db, "SELECT count FROM entity_rarity WHERE entity_key = 'Q1'") == 2


async def test_attach_is_idempotent(db: Database, cfg: Settings) -> None:
    a = await seed(db, "a", vec(1.0), entities=ACME)
    first = await attach(db, cfg, a)
    assert await attach(db, cfg, a) == first
    assert await scalar(db, "SELECT count(*) FROM narrative_groups") == 1
    assert await scalar(db, "SELECT count FROM entity_rarity WHERE entity_key = 'Q1'") == 1
    assert await scalar(db, "SELECT count(*) FROM narratives") == 1


async def test_a_merged_or_unknown_group_is_skipped(db: Database, cfg: Settings) -> None:
    a = await seed(db, "a", vec(1.0))
    b = await seed_group(db, "b", vec(0.9), state="merged", merged_into=a)
    await attach_narrative(
        ctx(db, cfg, names.JOB_ATTACH_NARRATIVE, {"group_id": str(await public_id(db, b))})
    )
    await attach_narrative(
        ctx(db, cfg, names.JOB_ATTACH_NARRATIVE, {"group_id": str(uuid.uuid4())})
    )
    assert await scalar(db, "SELECT count(*) FROM narratives") == 0


async def test_a_semantic_signal_alone_founds_a_new_narrative(db: Database, cfg: Settings) -> None:
    a = await seed(db, "a", vec(1.0))
    first = await attach(db, cfg, a)
    b = await seed(db, "b", vec(0.95))  # very close, but nothing else in common
    second = await attach(db, cfg, b)
    assert second != first


async def test_signals_without_a_specific_anchor_do_not_attach(db: Database, cfg: Settings) -> None:
    a = await seed(db, "a", vec(1.0), entities=ACME)
    first = await attach(db, cfg, a)
    async with db.transaction() as s:  # Acme is everywhere: not a specific anchor
        await s.execute(
            text(
                "INSERT INTO entity_rarity (entity_key, day, count) VALUES ('Q1', :d, 1000) "
                "ON CONFLICT (entity_key, day) DO UPDATE SET count = 1000"
            ),
            {"d": datetime.now(UTC).date()},
        )
    b = await seed(db, "b", vec(0.9), entities=ACME)
    second = await attach(db, cfg, b)
    assert second != first


async def test_a_low_medoid_similarity_blocks_the_attachment(db: Database) -> None:
    strict = make_settings(fc_narrative_medoid_floor=0.95)
    a = await seed(db, "a", vec(1.0), entities=ACME)
    await link(db, a, [f"Acme {LINK_A}"])
    first = await attach(db, strict, a)
    b = await seed(db, "b", vec(0.9), entities=ACME)
    await link(db, b, [f"Acme {LINK_A}"])
    assert await attach(db, strict, b) != first


async def test_cooccurrence_of_four_accounts_is_a_signal(db: Database, cfg: Settings) -> None:
    a = await seed(db, "a", vec(1.0))
    shared = await link(db, a, [f"Same post {LINK_A}"] * 4)
    first = await attach(db, cfg, a)
    b = await seed(db, "b", vec(0.9))
    for content_id in shared:  # the same four posts carry a claim of both groups
        await link_content(db, content_id, b)
    assert await attach(db, cfg, b) == first
    detail = (await rows(db, "SELECT signals FROM narrative_groups WHERE group_id = :g", g=b))[0][0]
    assert detail["signals"]["cooccurrence"] and detail["anchors"]["shared_link"]
    assert detail["cooccurrence_posts"] == 4 and detail["cooccurrence_accounts"] == 4


async def test_cooccurrence_needs_more_than_three_accounts(db: Database, cfg: Settings) -> None:
    a = await seed(db, "a", vec(1.0))
    shared = await link(db, a, [f"Same post {LINK_A}"] * 4, authors=["1", "2", "3", "3"])
    first = await attach(db, cfg, a)
    b = await seed(db, "b", vec(0.9))
    for content_id in shared:
        await link_content(db, content_id, b)
    assert await attach(db, cfg, b) != first  # 4 posts but only 3 accounts


async def test_a_shared_specific_media_attaches(db: Database, cfg: Settings) -> None:
    a = await seed(db, "a", vec(1.0))
    media = await add_media(db, await link(db, a, ["Photo of the event"]))
    first = await attach(db, cfg, a)
    b = await seed(db, "b", vec(0.9))
    await add_media(db, await link(db, b, ["The same photo again"]), media)
    assert await attach(db, cfg, b) == first
    detail = (await rows(db, "SELECT signals FROM narrative_groups WHERE group_id = :g", g=b))[0][0]
    assert detail["signals"]["media"] and detail["anchors"]["specific_media"]


async def test_a_generic_media_is_not_an_anchor(db: Database, cfg: Settings) -> None:
    a = await seed(db, "a", vec(1.0))
    media = await add_media(db, await link(db, a, ["Reaction meme"]))
    first = await attach(db, cfg, a)
    for i in range(3):  # the same image already illustrates three other narratives
        other = await seed(db, f"o{i}", unit(10 + i))
        await add_media(db, await link(db, other, [f"Other story {i}"]), media)
        await attach(db, cfg, other)
    b = await seed(db, "b", vec(0.9))
    await add_media(db, await link(db, b, ["Reaction meme again"]), media)
    assert await attach(db, cfg, b) != first


async def test_a_match_on_a_closed_narrative_founds_a_new_one_with_previous(
    db: Database, cfg: Settings
) -> None:
    a = await seed(db, "a", vec(1.0), entities=ACME)
    await link(db, a, [f"Acme {LINK_A}"])
    old = await attach(db, cfg, a)
    async with db.transaction() as s:
        await s.execute(
            text("UPDATE narratives SET state = 'closed', closed_at = now() WHERE id = :n"),
            {"n": old},
        )
    b = await seed(db, "b", vec(0.9), entities=ACME)
    await link(db, b, [f"Acme again {LINK_A}"])

    new = await attach(db, cfg, b)

    assert new != old
    row = (
        await rows(db, "SELECT previous_narrative_id, state FROM narratives WHERE id = :n", n=new)
    )[0]
    assert row[0] == old and row[1] == "active"
    assert await scalar(db, "SELECT state FROM narratives WHERE id = :n", n=old) == "closed"
    assert (
        await scalar(db, "SELECT count(*) FROM narrative_groups WHERE narrative_id = :n", n=old)
        == 1
    )
    created = (
        await rows(db, "SELECT reason FROM narrative_events WHERE narrative_id = :n", n=new)
    )[0]
    assert created[0] == "resurgence"


async def test_the_best_of_several_matching_narratives_wins(db: Database, cfg: Settings) -> None:
    a = await seed(db, "a", vec(1.0), entities=ACME)
    await link(db, a, [f"Acme {LINK_A}"])
    weak = await attach(db, cfg, a)
    c = await seed(db, "c", vec(0.85), entities=ACME)  # a second Acme narrative, less similar
    await link(db, c, [f"Other {LINK_A}"])
    other = await build_narrative(db, c)  # founds its own narrative, by hand
    assert other != weak
    b = await seed(db, "b", vec(0.99), entities=ACME)
    await link(db, b, [f"Acme once more {LINK_A}"])
    assert await attach(db, cfg, b) == weak  # same signals, higher medoid similarity


async def test_group_created_subscription_enqueues_one_job_per_group(
    db: Database, cfg: Settings
) -> None:
    a = await seed(db, "a", vec(1.0))
    public = str(await public_id(db, a))
    events = [
        event(names.CLAIM_GROUP_CREATED, {"group_id": public}),
        event(names.CLAIM_GROUP_CREATED, {"group_id": public}, id_=2),
        event(names.CLAIM_GROUP_CREATED, {"group_id": "not-a-uuid"}, id_=3),
        event(names.CLAIM_GROUP_CREATED, {}, id_=4),
    ]
    async with db.transaction() as s:
        await on_group_created(s, events)
        await on_group_created(s, [])
    queued = await jobs_of(db, names.JOB_ATTACH_NARRATIVE)
    assert len(queued) == 1
    assert queued[0]["group_id"] == public and queued[0]["_dedupe"] == f"narr:{a}"


# ------------------------------------------------------------------- maintenance


async def maintain(db: Database, settings: Settings) -> int:
    async with db.transaction() as s:
        return await maintain_narratives(s, settings)


async def test_inactive_narratives_close_after_the_configured_days(
    db: Database, cfg: Settings
) -> None:
    stale_group = await seed(db, "stale", unit(1))
    fresh_group = await seed(db, "fresh", unit(2))
    stale = await build_narrative(db, stale_group)
    fresh = await build_narrative(db, fresh_group)
    async with db.transaction() as s:
        await s.execute(
            text(
                "UPDATE narratives SET last_activity_at = now() - interval '8 days' WHERE id = :n"
            ),
            {"n": stale},
        )
        await s.execute(
            text(
                "UPDATE narratives SET last_activity_at = now() - interval '6 days' WHERE id = :n"
            ),
            {"n": fresh},
        )
    assert await maintain(db, cfg) >= 1
    assert await scalar(db, "SELECT state FROM narratives WHERE id = :n", n=stale) == "closed"
    assert await scalar(db, "SELECT closed_at IS NOT NULL FROM narratives WHERE id = :n", n=stale)
    assert await scalar(db, "SELECT state FROM narratives WHERE id = :n", n=fresh) == "active"
    assert "closed" in await kinds(db, stale)
    assert "closed" not in await kinds(db, fresh)
    # a shorter delay closes the second one too
    await maintain(db, make_settings(fc_narrative_close_days=5))
    assert await scalar(db, "SELECT state FROM narratives WHERE id = :n", n=fresh) == "closed"


async def test_medoids_are_re_elected_at_most_once_a_day(db: Database, cfg: Settings) -> None:
    gids = [await seed(db, f"g{i}", unit(20 + i), reach=r) for i, r in enumerate([0, 10, 90, 50])]
    nid = await build_narrative(db, *gids)
    # freshly created: medoids = [founder] and elected today, so nothing changes yet
    await maintain(db, cfg)
    assert list(
        await scalar(db, "SELECT medoid_group_ids FROM narratives WHERE id = :n", n=nid)
    ) == [gids[0]]
    async with db.transaction() as s:
        await s.execute(
            text(
                "UPDATE narratives SET medoids_elected_at = now() - interval '2 days' WHERE id = :n"
            ),
            {"n": nid},
        )
    assert await maintain(db, cfg) >= 1
    medoids = list(await scalar(db, "SELECT medoid_group_ids FROM narratives WHERE id = :n", n=nid))
    assert medoids == [gids[0], gids[2], gids[3]]  # founder, then highest reach
    assert "medoids_changed" in await kinds(db, nid)
    # reach changes right after: no new election before a day has passed
    async with db.transaction() as s:
        await s.execute(text("UPDATE claim_groups SET reach = 999 WHERE id = :g"), {"g": gids[1]})
    await maintain(db, cfg)
    assert (
        list(await scalar(db, "SELECT medoid_group_ids FROM narratives WHERE id = :n", n=nid))
        == medoids
    )


async def test_a_narrative_above_the_group_limit_gets_a_split_suggestion(db: Database) -> None:
    small = make_settings(fc_narrative_split_groups=3)
    gids = [await seed(db, f"s{i}", unit(30 + i)) for i in range(4)]
    big = await build_narrative(db, *gids)
    few = await build_narrative(db, await seed(db, "f", unit(40)))
    await maintain(db, small)
    assert await scalar(db, "SELECT split_suggested FROM narratives WHERE id = :n", n=big)
    assert not await scalar(db, "SELECT split_suggested FROM narratives WHERE id = :n", n=few)


async def test_a_bridging_group_suggests_a_split_until_the_subsets_reconnect(
    db: Database, cfg: Settings
) -> None:
    gids = [await seed(db, f"b{i}", unit(50 + i)) for i in range(5)]
    nid = await build_narrative(db, *gids)
    for left, right in [(0, 1), (1, 2), (2, 3), (3, 4)]:  # a chain: group 2 bridges
        (content,) = await link(db, gids[left], [f"post {left}-{right}"])
        await link_content(db, content, gids[right])
    await maintain(db, cfg)
    assert await scalar(db, "SELECT split_suggested FROM narratives WHERE id = :n", n=nid)
    (content,) = await link(db, gids[4], ["closing the loop"])
    await link_content(db, content, gids[0])  # the chain becomes a cycle
    await maintain(db, cfg)
    assert not await scalar(db, "SELECT split_suggested FROM narratives WHERE id = :n", n=nid)


async def test_old_stats_and_entity_rarity_are_purged(db: Database, cfg: Settings) -> None:
    nid = await build_narrative(db, await seed(db, "p", unit(60)))
    now = datetime.now(UTC)
    today = now.date()
    async with db.transaction() as s:
        for end in (now - timedelta(days=31), now - timedelta(days=2)):
            await s.execute(
                text(
                    "INSERT INTO narrative_stats (narrative_id, span, window_end, posts, "
                    "flow_posts, share, accounts, platforms, score) "
                    "VALUES (:n, '1h', :e, 1, 10, 0.1, 1, 1, 0)"
                ),
                {"n": nid, "e": end},
            )
        for key, day in (("old", today - timedelta(days=32)), ("new", today - timedelta(days=30))):
            await s.execute(
                text("INSERT INTO entity_rarity (entity_key, day, count) VALUES (:k, :d, 1)"),
                {"k": key, "d": day},
            )
    await maintain(db, cfg)
    assert await scalar(db, "SELECT count(*) FROM narrative_stats") == 1
    keys = {r[0] for r in await rows(db, "SELECT entity_key FROM entity_rarity")}
    assert keys == {"new"}
    assert date.today() >= today  # sanity: the day boundary did not move under the test


# ------------------------------------------------------------------------- title


class TitleLLM:
    def __init__(self, outcome: Any = None) -> None:
        self.outcome = outcome or NarrativeTitleOutput(
            title_fr="Allégations sur Acme", title_en="Claims about Acme"
        )
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return LLMResult(
            output=self.outcome,
            provider="fake",
            model="fake",
            usage=TokenUsage(),
            cost_usd_micros=0,
            free_tier=False,
            cache_hit=False,
            request_id="r",
        )


async def test_title_job_stores_a_french_draft_and_the_english_one_in_the_journal(
    db: Database, cfg: Settings
) -> None:
    gids = [await seed(db, f"Claim number {i}", unit(70 + i), reach=i) for i in range(7)]
    nid = await build_narrative(db, *gids)
    llm = TitleLLM()
    runtime.set_override("llm", llm)
    await narrative_title(ctx(db, cfg, names.JOB_NARRATIVE_TITLE, {"narrative_id": nid}))
    row = (await rows(db, "SELECT title, title_status FROM narratives WHERE id = :n", n=nid))[0]
    assert row[0] == "Allégations sur Acme" and row[1] == "draft"
    journal = (
        await rows(
            db,
            "SELECT data FROM narrative_events WHERE kind = 'title_set' AND narrative_id = :n",
            n=nid,
        )
    )[0][0]
    assert journal["title_en"] == "Claims about Acme" and journal["status"] == "draft"
    (request,) = llm.requests
    assert request.role == names.ROLE_NARRATIVE_TITLE
    sent = request.input["claims"]
    assert len(sent) == 5 and all("ˆ" in c for c in sent)  # datamarked, at most five
    assert sent[0].replace("ˆ", " ").split() == ["Claim", "number", "0"]  # founder first


async def test_title_job_never_overwrites_a_validated_title_and_survives_a_refusal(
    db: Database, cfg: Settings
) -> None:
    nid = await build_narrative(db, await seed(db, "Some claim", unit(80)))
    async with db.transaction() as s:
        await s.execute(
            text(
                "UPDATE narratives SET title = 'Validated', title_status = 'validated' "
                "WHERE id = :n"
            ),
            {"n": nid},
        )
    llm = TitleLLM()
    runtime.set_override("llm", llm)
    await narrative_title(ctx(db, cfg, names.JOB_NARRATIVE_TITLE, {"narrative_id": nid}))
    assert llm.requests == []
    assert await scalar(db, "SELECT title FROM narratives WHERE id = :n", n=nid) == "Validated"

    other = await build_narrative(db, await seed(db, "Another claim", unit(81)))
    runtime.set_override("llm", TitleLLM(AllRefused("refused")))
    await narrative_title(ctx(db, cfg, names.JOB_NARRATIVE_TITLE, {"narrative_id": other}))
    assert await scalar(db, "SELECT title_status FROM narratives WHERE id = :n", n=other) == "none"


async def test_validate_title_needs_review_permission_and_a_title(db: Database) -> None:
    nid = await build_narrative(db, await seed(db, "x", unit(90)))
    public = uuid.UUID(
        str(await scalar(db, "SELECT public_id FROM narratives WHERE id = :n", n=nid))
    )
    async with db.transaction() as s:
        with pytest.raises(PermissionDenied):
            await nar.validate_title(s, Actor.anonymous(), public)
    async with db.transaction() as s:
        with pytest.raises(InvalidInput) as empty:
            await nar.validate_title(s, SYSTEM, public)  # nothing to validate yet
    assert empty.value.code == "TWR-FCK-044"
    async with db.transaction() as s:
        with pytest.raises(NotFound) as missing:
            await nar.validate_title(s, SYSTEM, uuid.uuid4())
    assert missing.value.code == "TWR-FCK-043"
    async with db.transaction() as s:
        await s.execute(
            text("UPDATE narratives SET title = 'Draft', title_status = 'draft' WHERE id = :n"),
            {"n": nid},
        )
    async with db.transaction() as s:
        done = await nar.validate_title(s, SYSTEM, public)
    assert done.title == "Draft" and done.title_status == "validated"
    async with db.transaction() as s:
        edited = await nar.validate_title(s, SYSTEM, public, title="  Edited title ")
    assert edited.title == "Edited title"
    assert (await kinds(db, nid)).count("title_set") == 2


# ------------------------------------------------------------------ merge and split


async def narrative_public(db: Database, nid: int) -> uuid.UUID:
    return uuid.UUID(str(await scalar(db, "SELECT public_id FROM narratives WHERE id = :n", n=nid)))


async def test_merge_moves_the_groups_and_keeps_the_history(db: Database) -> None:
    a, b, c = await seed_many(db, 3, 100, reach=1)
    target = await build_narrative(db, a)
    source = await build_narrative(db, b, c)
    t_public, s_public = await narrative_public(db, target), await narrative_public(db, source)
    async with db.transaction() as s:
        merged = await nar.merge_narratives(
            s, SYSTEM, target_id=t_public, source_id=s_public, reason="same story"
        )
    assert merged.id == target
    assert await narrative_of(db, b) == target and await narrative_of(db, c) == target
    old_rows = await rows(
        db,
        "SELECT group_id FROM narrative_groups WHERE narrative_id = :n AND removed_at IS NOT NULL",
        n=source,
    )
    assert {r[0] for r in old_rows} == {b, c}  # history kept with a removal date
    src = (
        await rows(
            db,
            "SELECT state, merged_into_id, closed_at IS NOT NULL FROM narratives WHERE id = :n",
            n=source,
        )
    )[0]
    assert src[0] == "closed" and src[1] == target and src[2]
    assert "merged" in await kinds(db, target) and "merged" in await kinds(db, source)
    (payload,) = await events_of(db, names.NARRATIVE_MERGED)
    assert payload["target_id"] == str(t_public) and payload["source_id"] == str(s_public)
    assert len(payload["group_ids"]) == 2 and payload["reason"] == "same story"
    assert payload["actor"] == SYSTEM.label
    medoids = list(
        await scalar(db, "SELECT medoid_group_ids FROM narratives WHERE id = :n", n=target)
    )
    assert medoids == [a, c, b]  # re-elected: founder, then highest reach


async def test_merge_rejects_bad_requests(db: Database) -> None:
    a, b = await seed(db, "a", unit(110)), await seed(db, "b", unit(111))
    one, two = await build_narrative(db, a), await build_narrative(db, b)
    p1, p2 = await narrative_public(db, one), await narrative_public(db, two)

    async def merge(actor: Actor, target: uuid.UUID, source: uuid.UUID) -> None:
        async with db.transaction() as s:
            await nar.merge_narratives(s, actor, target_id=target, source_id=source, reason="r")

    with pytest.raises(PermissionDenied):
        await merge(Actor.anonymous(), p1, p2)
    with pytest.raises(InvalidInput) as same:
        await merge(SYSTEM, p1, p1)
    assert same.value.code == ec.FCK_NARRATIVE_INVALID.code
    with pytest.raises(NotFound) as unknown:
        await merge(SYSTEM, p1, uuid.uuid4())
    assert unknown.value.code == ec.FCK_NARRATIVE_NOT_FOUND.code
    async with db.transaction() as s:
        await s.execute(text("UPDATE narratives SET state = 'closed' WHERE id = :n"), {"n": two})
    with pytest.raises(InvalidInput) as closed:
        await merge(SYSTEM, p1, p2)
    assert closed.value.code == "TWR-FCK-044"
    assert await scalar(db, "SELECT count(*) FROM events WHERE topic = 'narrative.merged'") == 0


async def test_split_moves_groups_to_a_new_narrative(db: Database) -> None:
    a, b, c, d = await seed_many(db, 4, 120, reach=10)
    nid = await build_narrative(db, a, b, c, d)
    public = await narrative_public(db, nid)
    async with db.transaction() as s:
        await s.execute(
            text(
                "UPDATE narratives SET split_suggested = true, medoid_group_ids = :m WHERE id = :n"
            ),
            {"m": [a, c, d], "n": nid},
        )
    b_public, c_public = await public_id(db, b), await public_id(db, c)
    async with db.transaction() as s:
        created = await nar.split_narrative(
            s, SYSTEM, narrative_id=public, group_ids=[b_public, c_public], reason="two stories"
        )
    assert created.founder_group_id == c  # highest reach among the moved groups
    assert await narrative_of(db, b) == created.id and await narrative_of(db, c) == created.id
    assert await narrative_of(db, a) == nid and await narrative_of(db, d) == nid
    old = await rows(
        db,
        "SELECT group_id FROM narrative_groups WHERE narrative_id = :n AND removed_at IS NOT NULL",
        n=nid,
    )
    assert {r[0] for r in old} == {b, c}
    assert not await scalar(db, "SELECT split_suggested FROM narratives WHERE id = :n", n=nid)
    # c was a medoid of the source: the source elected again without it
    assert c not in list(
        await scalar(db, "SELECT medoid_group_ids FROM narratives WHERE id = :n", n=nid)
    )
    assert "split" in await kinds(db, nid) and "split" in await kinds(db, created.id)
    (payload,) = await events_of(db, names.NARRATIVE_SPLIT)
    assert payload["narrative_id"] == str(public) and payload["new_narrative_id"] == str(
        created.public_id
    )
    assert set(payload["group_ids"]) == {str(b_public), str(c_public)}
    assert payload["actor"] == SYSTEM.label and payload["reason"] == "two stories"


async def test_split_rejects_bad_requests(db: Database) -> None:
    a, b, c = await seed_many(db, 3, 130)
    outsider = await seed(db, "o", unit(140))
    nid = await build_narrative(db, a, b, c)
    public = await narrative_public(db, nid)
    pa, pb, pc, po = [await public_id(db, g) for g in (a, b, c, outsider)]

    async def split(actor: Actor, groups: list[uuid.UUID], narrative: uuid.UUID = public) -> None:
        async with db.transaction() as s:
            await nar.split_narrative(
                s, actor, narrative_id=narrative, group_ids=groups, reason="r"
            )

    with pytest.raises(PermissionDenied):
        await split(Actor.anonymous(), [pb])
    for bad in ([], [pa], [pb, pc, pa], [po], [pb, uuid.uuid4()]):  # empty, founder, all, outsider
        with pytest.raises(InvalidInput) as err:
            await split(SYSTEM, bad)
        assert err.value.code == "TWR-FCK-044"
    with pytest.raises(NotFound):
        await split(SYSTEM, [pb], uuid.uuid4())
    assert await scalar(db, "SELECT count(*) FROM narratives") == 1
    assert await scalar(db, "SELECT count(*) FROM events WHERE topic = 'narrative.split'") == 0


async def test_detach_closes_the_membership_but_never_removes_the_founder(db: Database) -> None:
    a, b = await seed(db, "a", unit(150)), await seed(db, "b", unit(151))
    nid = await build_narrative(db, a, b)
    async with db.transaction() as s:
        narrative = await s.get(Narrative, nid)
        group_b, group_a = await s.get(ClaimGroup, b), await s.get(ClaimGroup, a)
        assert narrative is not None and group_a is not None and group_b is not None
        assert await nar.detach_group(s, SYSTEM, narrative, group_b, reason="wrong story")
        assert not await nar.detach_group(s, SYSTEM, narrative, group_b, reason="again")
        with pytest.raises(InvalidInput):
            await nar.detach_group(s, SYSTEM, narrative, group_a, reason="founder")
    assert (
        await rows(
            db, "SELECT 1 FROM narrative_groups WHERE group_id = :g AND removed_at IS NULL", g=b
        )
        == []
    )
    assert "detached" in await kinds(db, nid)
