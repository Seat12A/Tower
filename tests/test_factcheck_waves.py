"""Waves (real DB): window stats, tiers and their effects, surge end, surge quota and TTL cap,
surge sampling and novelty."""

from __future__ import annotations

import itertools
import random
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain.ingestion import AuthorInput, ItemInput
from tower.core.factcheck import names, runtime
from tower.core.models.factcheck import ClaimGroup, EvidencePassage, Investigation, Source
from tower.core.models.factcheck_v1 import ContentEmbedding
from tower.core.models.ingestion import ExtensionFilterVersion
from tower.core.usecases import drafts as drafts_uc
from tower.core.usecases import ingestion as ing
from tower.services.factcheck.jobs import assemble
from tower.services.factcheck.narratives import filter_for_surges, waves_step
from tower.services.factcheck.periodic import drain_quota
from tower.services.factcheck.routing import embedding_partitions

from .conftest import make_settings
from .test_factcheck_matching import (
    _reset,  # noqa: F401  (autouse fixture)
    events_of,
    vec,
)
from .test_factcheck_narratives import (
    SYSTEM,
    build_narrative,
    kinds,
    narrative_public,
    seed,
    unit,
)
from .test_factcheck_service import (
    POST,
    ctx,
    jobs_of,
    make_content,
    rows,
    scalar,
    seed_investigation,
)

_ext = itertools.count(940_000)
_author = itertools.count(1)


def now_utc() -> datetime:
    return datetime.now(UTC)


async def bulk(
    db: Database,
    gid: int | None,
    count: int,
    *,
    platform: str = "x",
    observed: datetime | None = None,
    metrics: dict[str, int] | None = None,
    label: str = "Report",
    authors: int | None = None,
) -> list[int]:
    """``count`` posts (one author each unless ``authors`` caps the accounts), observed at
    ``observed`` (default: 12 minutes ago), linked to the claim of ``gid`` when given."""
    when = observed or now_utc() - timedelta(minutes=12)
    first = next(_ext)
    for _ in range(count - 1):
        next(_ext)
    externals: list[str] = []
    items: list[ItemInput] = []
    for i in range(count):
        ext = str(first + i) if platform == "x" else f"w{first + i}"
        externals.append(ext)
        who = next(_author) if authors is None else (i % authors) + 1_000_000
        items.append(
            ItemInput(
                platform=platform,
                external_id=ext,
                text=f"{label} {first + i} " + " ".join(uuid.uuid4().hex[:7] for _ in range(6)),
                author=AuthorInput(external_id=f"{who}", handle=f"user{who}"),
                lang_hint="fr",
                created_at=when,
                observed_at=when,
                metrics=metrics or {},
            )
        )
    for start in range(0, len(items), 100):
        async with db.transaction() as session:
            await ing.ingest_items(
                session,
                SYSTEM,
                items[start : start + 100],
                source="radar",
                filter_version=None,
                installation_token=None,
                max_items=100,
                daily_quota=1_000_000,
            )
    found = await rows(
        db,
        "SELECT id FROM content_items WHERE platform = :p AND external_id = ANY(:e) ORDER BY id",
        p=platform,
        e=externals,
    )
    ids = [int(r[0]) for r in found]
    if gid is not None:
        async with db.transaction() as s:
            await s.execute(
                text(
                    "INSERT INTO content_claims (content_id, claim_id, position) "
                    "SELECT c, (SELECT representative_claim_id FROM claim_groups WHERE id = :g), 1 "
                    "FROM unnest(CAST(:ids AS bigint[])) c ON CONFLICT DO NOTHING"
                ),
                {"g": gid, "ids": ids},
            )
    return ids


async def wave(db: Database, settings: Settings, now: datetime | None = None) -> dict[str, Any]:
    async with db.transaction() as s:
        return await waves_step(s, settings, now or now_utc())


async def state_of(db: Database, nid: int) -> str:
    return str(await scalar(db, "SELECT state FROM narratives WHERE id = :n", n=nid))


async def set_narrative(db: Database, nid: int, **columns: Any) -> None:
    sets = ", ".join(f"{k} = :{k}" for k in columns)
    async with db.transaction() as s:
        await s.execute(text(f"UPDATE narratives SET {sets} WHERE id = :n"), {"n": nid, **columns})  # noqa: S608


async def two_group_narrative(
    db: Database, cfg: Settings, *, main: int, side: int, theme: str | None = None
) -> tuple[int, int, int, list[int]]:
    """Narrative of two groups (reach 100 and 5); ``main`` posts on the first, ``side`` on
    the second. Returns (narrative, group1, group2, all content ids)."""
    g1 = await seed(db, "Main claim", unit(200), reach=100, theme=theme)
    g2 = await seed(db, "Side claim", unit(201), reach=5, theme=theme)
    nid = await build_narrative(db, g1, g2)
    contents = await bulk(db, g1, main) + await bulk(db, g2, side)
    return nid, g1, g2, contents


@pytest.fixture
def cfg() -> Settings:
    return make_settings()


@pytest.fixture(autouse=True)
def _runtime() -> Iterator[None]:
    runtime.clear_overrides()
    yield
    runtime.clear_overrides()


# ------------------------------------------------------------------------- stats


async def test_window_stats_are_written_per_span(db: Database, cfg: Settings) -> None:
    g = await seed(db, "Claim", unit(210))
    nid = await build_narrative(db, g)
    await bulk(db, g, 20, metrics={"impressions": 1000})
    await bulk(db, None, 180)  # the rest of the observed flow
    result = await wave(db, cfg)
    assert result["narratives"] == 1 and result["stats_rows"] == 2
    stats = {
        r[0]: r
        for r in await rows(
            db,
            "SELECT span, posts, flow_posts, share, accounts, platforms, reach, score "
            "FROM narrative_stats WHERE narrative_id = :n",
            n=nid,
        )
    }
    assert set(stats) == {"1h", "6h"}  # nothing in the last 5 minutes: no row
    _, posts, flow, share, accounts, platforms, reach, score = stats["1h"]
    assert (posts, flow, accounts, platforms, reach) == (20, 200, 20, 1, 20_000)
    assert share == pytest.approx(0.1) and score == pytest.approx(20 / 5**0.5)
    assert stats["6h"][1] == 20


async def test_exact_copies_count_through_their_original(db: Database, cfg: Settings) -> None:
    # Mass-flow copies are never extracted: they carry no claim but must count in the wave.
    g = await seed(db, "Claim", unit(211))
    nid = await build_narrative(db, g)
    (original,) = await bulk(db, g, 1)
    copies = await bulk(db, None, 9)
    async with db.transaction() as s:
        await s.execute(
            text("UPDATE content_items SET copy_of_id = :o WHERE id = ANY(:ids)"),
            {"o": original, "ids": copies},
        )
    await bulk(db, None, 90)
    await wave(db, cfg)
    posts, accounts = (
        await rows(
            db,
            "SELECT posts, accounts FROM narrative_stats WHERE narrative_id = :n AND span = '1h'",
            n=nid,
        )
    )[0]
    assert (posts, accounts) == (10, 10)


async def test_the_score_compares_with_the_previous_window_scaled_by_the_flow(
    db: Database, cfg: Settings
) -> None:
    g = await seed(db, "Claim", unit(211))
    nid = await build_narrative(db, g)
    previous = now_utc() - timedelta(minutes=90)
    await bulk(db, g, 10, observed=previous)
    await bulk(db, None, 90, observed=previous)  # previous hour: 10 of 100
    await bulk(db, g, 20)
    await bulk(db, None, 180)  # this hour: 20 of 200, same share
    await wave(db, cfg)
    score = await scalar(
        db, "SELECT score FROM narrative_stats WHERE narrative_id = :n AND span = '1h'", n=nid
    )
    assert score == pytest.approx(0.0)  # expected = 10 * 200 / 100 = 20, observed = 20


# ------------------------------------------------------------------------ tiers


async def test_watch_tier_marks_the_narrative_and_asks_for_a_title(
    db: Database, cfg: Settings
) -> None:
    g = await seed(db, "Claim", unit(220))
    nid = await build_narrative(db, g)
    await bulk(db, g, 12)
    await bulk(db, None, 988)  # 12 of 1000 = 1.2 %, 12 accounts
    first = await wave(db, cfg)
    assert first["watch"] == 1 and first["titles"] == 1 and first["surge_entered"] == 0
    assert await state_of(db, nid) == "watch"
    assert await kinds(db, nid) == ["created", "state_changed"]
    assert [j["narrative_id"] for j in await jobs_of(db, names.JOB_NARRATIVE_TITLE)] == [nid]
    assert (await jobs_of(db, names.JOB_NARRATIVE_TITLE))[0]["_dedupe"] == f"title:{nid}"
    assert await events_of(db, names.NARRATIVE_SURGE_DETECTED) == []
    second = await wave(db, cfg)  # nothing moves on the next pass
    assert second["watch"] == 0 and second["titles"] == 0
    assert len(await jobs_of(db, names.JOB_NARRATIVE_TITLE)) == 1
    assert await kinds(db, nid) == ["created", "state_changed"]
    # a narrative that already has a title is not asked again
    async with db.transaction() as s:
        await s.execute(
            text("UPDATE narratives SET title = 'T', title_status = 'validated' WHERE id = :n"),
            {"n": nid},
        )
        await s.execute(text("DELETE FROM jobs"))
    assert (await wave(db, cfg))["titles"] == 0


async def test_a_quiet_narrative_stays_active(db: Database, cfg: Settings) -> None:
    g = await seed(db, "Claim", unit(221))
    nid = await build_narrative(db, g)
    await bulk(db, g, 5)
    await bulk(db, None, 995)
    await wave(db, cfg)
    assert await state_of(db, nid) == "active"
    assert await jobs_of(db, names.JOB_NARRATIVE_TITLE) == []


async def test_surge_applies_wave_mode(db: Database, cfg: Settings) -> None:
    nid, g1, g2, _ = await two_group_narrative(db, cfg, main=30, side=10)
    await bulk(db, None, 960)  # 40 of 1000 = 4 %, 40 accounts, from zero: growth
    result = await wave(db, cfg)

    assert result["surge_entered"] == 1 and result["groups_raised"] == 2
    assert result["investigations"] == 1 and result["titles"] == 1
    row = (
        await rows(
            db,
            "SELECT state, surge_started_at IS NOT NULL, representative_group_id, "
            "below_watch_since FROM narratives WHERE id = :n",
            n=nid,
        )
    )[0]
    assert row[0] == "surge" and row[1] and row[2] == g1 and row[3] is None
    levels = {r[0]: r[1] for r in await rows(db, "SELECT id, level FROM claim_groups")}
    assert levels == {g1: "N2", g2: "N2"}
    changed = await events_of(db, names.CLAIM_GROUP_LEVEL_CHANGED)
    assert len(changed) == 2 and {e["reason"] for e in changed} == {"wave"}
    assert {e["to_level"] for e in changed} == {"N2"}
    # the representative group (highest reach) is investigated once, as a wave investigation
    invs = await rows(db, "SELECT claim_group_id, level, requested_by, flags FROM investigations")
    assert len(invs) == 1 and invs[0][0] == g1 and invs[0][1] == "N2"
    assert invs[0][2] == names.FACTCHECK_ACTOR
    assert invs[0][3]["wave"] is True and invs[0][3]["narrative_id"]
    assert await scalar(db, "SELECT state FROM claim_groups WHERE id = :g", g=g1) == "queued"
    assert await scalar(db, "SELECT state FROM claim_groups WHERE id = :g", g=g2) == "dormant"
    (detected,) = await events_of(db, names.NARRATIVE_SURGE_DETECTED)
    public = await narrative_public(db, nid)
    group_public = await scalar(db, "SELECT public_id FROM claim_groups WHERE id = :g", g=g1)
    assert detected == {
        "narrative_id": str(public),
        "tier": "surge",
        "share": pytest.approx(0.04),
        "accounts": 40,
        "platforms": ["x"],
        "title": None,
        "representative_group_id": str(group_public),
    }
    assert [j["narrative_id"] for j in await jobs_of(db, names.JOB_NARRATIVE_TITLE)] == [nid]
    assert await jobs_of(db, names.JOB_LIGHT_CHECK) == []  # no dossier yet
    state_event = (
        await rows(
            db,
            "SELECT from_state, to_state, reason, data FROM narrative_events "
            "WHERE kind = 'state_changed' AND narrative_id = :n",
            n=nid,
        )
    )[0]
    assert state_event[:3] == ("active", "surge", "wave") and state_event[3]["accounts"] == 40

    again = await wave(db, cfg)  # steady state: no second investigation, no second event
    assert again["investigations"] == 0 and again["surge_entered"] == 0
    assert await scalar(db, "SELECT count(*) FROM investigations") == 1
    assert len(await events_of(db, names.NARRATIVE_SURGE_DETECTED)) == 1


async def test_alert_raises_to_n4_and_starts_a_reinforced_investigation(
    db: Database, cfg: Settings
) -> None:
    nid, g1, _g2, _ = await two_group_narrative(db, cfg, main=60, side=40)
    await bulk(db, None, 10)  # 100 of 110: 91 % of the flow, 100 accounts
    result = await wave(db, cfg)
    assert result["alert_entered"] == 1
    assert await state_of(db, nid) == "alert"
    assert {r[0] for r in await rows(db, "SELECT level FROM claim_groups")} == {"N4"}
    (inv,) = await rows(db, "SELECT claim_group_id, level, depth FROM investigations")
    assert inv[0] == g1 and inv[1] == "N4" and inv[2] == "reinforced"
    (detected,) = await events_of(db, names.NARRATIVE_SURGE_DETECTED)
    assert detected["tier"] == "alert" and detected["accounts"] == 100
    assert {e["to_level"] for e in await events_of(db, names.CLAIM_GROUP_LEVEL_CHANGED)} == {"N4"}


async def test_a_surge_can_climb_to_alert_without_a_second_started_at(
    db: Database, cfg: Settings
) -> None:
    nid, *_ = await two_group_narrative(db, cfg, main=30, side=10)
    await bulk(db, None, 960)
    await wave(db, cfg)
    started = await scalar(db, "SELECT surge_started_at FROM narratives WHERE id = :n", n=nid)
    await set_narrative(db, nid, state="surge")
    extra = await seed(db, "Extra", unit(202))
    async with db.transaction() as s:
        await s.execute(
            text("INSERT INTO narrative_groups (narrative_id, group_id) VALUES (:n, :g)"),
            {"n": nid, "g": extra},
        )
    await bulk(db, extra, 60)  # 100 narrative posts of ~1060: 9.4 %, 100 accounts
    await wave(db, cfg)
    assert await state_of(db, nid) == "alert"
    assert (
        await scalar(db, "SELECT surge_started_at FROM narratives WHERE id = :n", n=nid) == started
    )
    tiers = [e["tier"] for e in await events_of(db, names.NARRATIVE_SURGE_DETECTED)]
    assert tiers == ["surge", "alert"]


async def test_watch_on_two_platforms_is_a_surge(db: Database, cfg: Settings) -> None:
    g = await seed(db, "Claim", unit(230))
    nid = await build_narrative(db, g)
    await bulk(db, g, 12)
    await bulk(db, g, 12, platform="bluesky")
    await bulk(db, None, 1000)
    await bulk(db, None, 100, platform="bluesky")  # 24 of ~1136: 2.1 %: no share rule
    await wave(db, cfg)
    assert await state_of(db, nid) == "surge"
    (detected,) = await events_of(db, names.NARRATIVE_SURGE_DETECTED)
    assert detected["platforms"] == ["bluesky", "x"]


async def test_a_group_at_n3_makes_a_watched_narrative_a_surge(db: Database, cfg: Settings) -> None:
    g = await seed(db, "Claim", unit(231), level="N3")
    nid = await build_narrative(db, g)
    await bulk(db, g, 12)
    await bulk(db, None, 988)
    await wave(db, cfg)
    assert await state_of(db, nid) == "surge"


async def test_a_watched_theme_goes_straight_to_alert(db: Database, cfg: Settings) -> None:
    g = await seed(db, "Claim", unit(232), theme="health")
    nid = await build_narrative(db, g)
    await bulk(db, g, 12)
    await bulk(db, None, 988)
    await wave(db, cfg)
    assert await state_of(db, nid) == "alert"
    other = await seed(db, "Other", unit(233), theme="culture")
    other_nid = await build_narrative(db, other)
    await bulk(db, other, 12)
    await wave(db, cfg)
    assert await state_of(db, other_nid) in ("watch", "surge")  # not a watched theme


async def test_growth_is_ignored_for_24_hours_after_a_filter_version_change(
    db: Database, cfg: Settings
) -> None:
    g = await seed(db, "Claim", unit(234))
    nid = await build_narrative(db, g)
    await bulk(db, g, 40)
    await bulk(db, None, 960)  # 4 %, 40 accounts, growth from zero: a surge...
    async with db.transaction() as s:
        s.add(ExtensionFilterVersion(version="v2", filters={}, active=True, created_by="test"))
    await wave(db, cfg)
    assert await state_of(db, nid) == "watch"  # ...unless the filters just changed
    async with db.transaction() as s:
        await s.execute(
            text("UPDATE extension_filter_versions SET created_at = now() - interval '25 hours'")
        )
    await wave(db, cfg)
    assert await state_of(db, nid) == "surge"


async def test_light_checks_wait_for_kept_passages_of_the_representative_dossier(
    db: Database, cfg: Settings
) -> None:
    _nid, _g1, g2, _ = await two_group_narrative(db, cfg, main=30, side=10)
    await bulk(db, None, 960)
    await wave(db, cfg)  # surge: the representative (g1) is investigated
    inv_id = int(await scalar(db, "SELECT id FROM investigations"))
    await wave(db, cfg)
    assert await jobs_of(db, names.JOB_LIGHT_CHECK) == []  # investigation still running

    async with db.transaction() as s:
        investigation = await s.get(Investigation, inv_id)
        assert investigation is not None
        investigation.stage = "completed"
        investigation.completed_at = now_utc() - timedelta(minutes=1)
    assert (await wave(db, cfg))["light_checks"] == 0  # completed but nothing kept yet

    async with db.transaction() as s:
        source = Source(
            canonical_url="https://insee.example/a",
            original_url="https://insee.example/a",
            adapter="brave",
            domain="insee.example",
            tier="primary",
        )
        s.add(source)
        await s.flush()
        s.add(
            EvidencePassage(
                investigation_id=inv_id,
                source_id=source.id,
                label="P1",
                position=0,
                text="Figures",
                lang="fr",
                simhash=1,
                kept=True,
            )
        )
    result = await wave(db, cfg)
    assert result["light_checks"] == 1  # the other group only: never the representative
    (job,) = await jobs_of(db, names.JOB_LIGHT_CHECK)
    assert job["group_id"] == g2 and job["dossier_investigation_id"] == inv_id
    assert job["_dedupe"] == f"light:{g2}:0"
    assert (await wave(db, cfg))["light_checks"] == 0  # still queued: deduplicated

    async with db.transaction() as s:  # the light check ran after the dossier: done
        await s.execute(text("DELETE FROM jobs"))
        await s.execute(
            text("UPDATE claim_groups SET light_checked_at = now() WHERE id = :g"), {"g": g2}
        )
    assert (await wave(db, cfg))["light_checks"] == 0
    assert await jobs_of(db, names.JOB_LIGHT_CHECK) == []


# ---------------------------------------------------------------------- surge end


async def test_a_surge_ends_after_the_hold_time_with_a_report(db: Database, cfg: Settings) -> None:
    nid, g1, g2, _ = await two_group_narrative(db, cfg, main=0, side=0)
    started = now_utc() - timedelta(hours=8)
    observed = started + timedelta(hours=1)
    contents = await bulk(db, g1, 5, observed=observed, platform="x")
    contents += await bulk(db, g2, 3, observed=observed, platform="bluesky")
    async with db.transaction() as s:
        investigation = Investigation(
            public_id=uuid.uuid4(),
            claim_group_id=g1,
            content_id=contents[0],
            level="N2",
            depth="standard",
            stage="completed",
            requested_by="system:factcheck",
            cost_usd_micros=4321,
        )
        s.add(investigation)
        await s.flush()
        await s.execute(
            text(
                "INSERT INTO verdicts (public_id, claim_group_id, investigation_id, veracity, "
                "context_need, status, drafted_at) VALUES (gen_random_uuid(), :g, :i, 'false', "
                "'none', 'draft', :d)"
            ),
            {"g": g1, "i": investigation.id, "d": started + timedelta(minutes=45)},
        )
    await set_narrative(
        db,
        nid,
        state="surge",
        surge_started_at=started,
        below_watch_since=now_utc() - timedelta(hours=2),
        representative_group_id=g1,
    )

    held = await wave(db, cfg)  # 2 h below watch of the 6 h required
    assert held["surge_ended"] == 0 and await state_of(db, nid) == "surge"
    assert await scalar(
        db, "SELECT below_watch_since IS NOT NULL FROM narratives WHERE id = :n", n=nid
    )
    assert await events_of(db, names.NARRATIVE_SURGE_ENDED) == []

    await set_narrative(db, nid, below_watch_since=now_utc() - timedelta(hours=7))
    ended = await wave(db, cfg)

    assert ended["surge_ended"] == 1 and await state_of(db, nid) == "active"
    assert await scalar(db, "SELECT below_watch_since FROM narratives WHERE id = :n", n=nid) is None
    (report,) = await events_of(db, names.NARRATIVE_SURGE_ENDED)
    assert report["narrative_id"] == str(await narrative_public(db, nid))
    assert report["duration_s"] == pytest.approx(8 * 3600, abs=60)
    assert report["posts"] == 8 and report["platforms"] == ["bluesky", "x"]
    assert report["cost_usd_micros"] == 4321
    assert report["first_draft_delay_s"] == pytest.approx(45 * 60, abs=2)
    ended_row = (
        await rows(
            db,
            "SELECT from_state, to_state, data FROM narrative_events "
            "WHERE kind = 'state_changed' AND to_state = 'active'",
        )
    )[0]
    assert ended_row[:2] == ("surge", "active") and ended_row[2]["report"]["posts"] == 8


async def test_the_clock_starts_when_a_surge_first_falls_below_watch(
    db: Database, cfg: Settings
) -> None:
    nid, _g1, *_ = await two_group_narrative(db, cfg, main=0, side=0)
    await set_narrative(db, nid, state="alert", surge_started_at=now_utc() - timedelta(hours=1))
    await wave(db, cfg)
    assert await state_of(db, nid) == "alert"
    assert await scalar(
        db, "SELECT below_watch_since IS NOT NULL FROM narratives WHERE id = :n", n=nid
    )


# --------------------------------------------------------------- quota and TTL cap


async def surge_group(db: Database, ext: str, *, in_surge: bool) -> Any:
    """A completed investigation with a draft verdict; its group sits in a narrative at
    ``surge`` (or ``active``)."""
    cid = await make_content(db, ext, POST)
    async with db.transaction() as s:
        inv = await seed_investigation(s, cid)
    group_id = int(inv.claim_group_id)
    nid = await build_narrative(db, group_id)
    await set_narrative(db, nid, state="surge" if in_surge else "active")
    return inv


async def drafted(db: Database, investigation_id: int) -> tuple[bool, bool]:
    row = (
        await rows(
            db,
            "SELECT drafted_at IS NOT NULL, coalesce(flags->>'surge', '') = 'true' "
            "FROM verdicts WHERE investigation_id = :i",
            i=investigation_id,
        )
    )[0]
    return bool(row[0]), bool(row[1])


async def run_assemble(db: Database, settings: Settings, investigation_id: int) -> None:
    await assemble(ctx(db, settings, names.JOB_ASSEMBLE, {"investigation_id": investigation_id}))


async def test_surge_drafts_use_their_own_quota(db: Database) -> None:
    settings = make_settings(fc_draft_daily_quota=1, fc_surge_daily_quota=1)
    runtime.set_override("platform:x", None)
    surge1 = await surge_group(db, "4501", in_surge=True)
    surge2 = await surge_group(db, "4502", in_surge=True)
    normal1 = await surge_group(db, "4503", in_surge=False)
    normal2 = await surge_group(db, "4504", in_surge=False)

    await run_assemble(db, settings, surge1.id)
    await run_assemble(db, settings, normal1.id)  # the daily slot is still free
    await run_assemble(db, settings, surge2.id)  # surge quota full: daily rules, and it is used
    await run_assemble(db, settings, normal2.id)

    assert await drafted(db, surge1.id) == (True, True)  # counted against the surge quota
    assert await drafted(db, normal1.id) == (True, False)
    assert await drafted(db, surge2.id) == (False, False)  # waits for a daily slot
    assert await drafted(db, normal2.id) == (False, False)
    flags = await scalar(db, "SELECT flags FROM verdicts WHERE investigation_id = :i", i=surge2.id)
    assert "over_quota_since" in flags and "surge" not in flags


async def test_a_full_surge_quota_falls_back_to_the_daily_quota(db: Database) -> None:
    settings = make_settings(fc_draft_daily_quota=5, fc_surge_daily_quota=1)
    runtime.set_override("platform:x", None)
    first = await surge_group(db, "4511", in_surge=True)
    second = await surge_group(db, "4512", in_surge=True)
    await run_assemble(db, settings, first.id)
    await run_assemble(db, settings, second.id)
    assert await drafted(db, first.id) == (True, True)
    assert await drafted(db, second.id) == (True, False)  # daily quota took it


async def test_a_zero_surge_quota_means_daily_rules_only(db: Database) -> None:
    settings = make_settings(fc_draft_daily_quota=5, fc_surge_daily_quota=0)
    runtime.set_override("platform:x", None)
    inv = await surge_group(db, "4521", in_surge=True)
    await run_assemble(db, settings, inv.id)
    assert await drafted(db, inv.id) == (True, False)


async def test_drain_does_not_spend_daily_slots_on_surge_drafts(db: Database) -> None:
    settings = make_settings(fc_draft_daily_quota=1, fc_surge_daily_quota=1)
    runtime.set_override("platform:x", None)
    surge = await surge_group(db, "4531", in_surge=True)
    waiting = await surge_group(db, "4532", in_surge=False)
    await run_assemble(db, settings, surge.id)
    async with db.transaction() as s:  # the second one waits (quota 0 at assembly time)
        await run_assemble_quota_zero(db, waiting.id)
    assert await drafted(db, waiting.id) == (False, False)
    async with db.transaction() as s:
        assert await drain_quota(s, settings) == 1  # the surge draft did not use the daily slot
    assert await drafted(db, waiting.id) == (True, False)


async def run_assemble_quota_zero(db: Database, investigation_id: int) -> None:
    await run_assemble(
        db, make_settings(fc_draft_daily_quota=0, fc_surge_daily_quota=0), investigation_id
    )


async def test_a_verdict_validated_in_a_surge_expires_within_the_surge_ttl(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(fc_surge_verdict_ttl_hours=2)
    monkeypatch.setattr(drafts_uc, "get_settings", lambda: settings)
    surging = await surge_group(db, "4541", in_surge=True)
    calm = await surge_group(db, "4542", in_surge=False)

    async def expiry(inv: Any) -> datetime:
        async with db.session() as s:
            group = await s.get(ClaimGroup, inv.claim_group_id)
            assert group is not None
            return await drafts_uc._expiry_of(s, group, None)

    soon = await expiry(surging)
    normal = await expiry(calm)
    assert timedelta(hours=1, minutes=59) < soon - now_utc() <= timedelta(hours=2, seconds=5)
    assert normal - now_utc() > timedelta(hours=2, minutes=5)  # the usual TTL is longer
    await set_narrative(db, int(await scalar(db, "SELECT min(id) FROM narratives")), state="active")
    assert await expiry(surging) - now_utc() > timedelta(hours=2, minutes=5)  # surge over: no cap


# ------------------------------------------------------------- sampling and novelty


async def embed(db: Database, content_id: int, vector: list[float]) -> None:
    async with db.transaction() as s:
        await embedding_partitions(s, make_settings())
    async with db.transaction() as s:
        s.add(
            ContentEmbedding(
                content_id=content_id, embedding=vector, model="m", lang="fr", level="N1"
            )
        )


async def surging_narrative(db: Database) -> tuple[int, int]:
    """A narrative at surge whose single medoid group has the vector ``vec(1.0)``."""
    g = await seed(db, "Surging claim", vec(1.0), level="N2")
    nid = await build_narrative(db, g)
    await set_narrative(db, nid, state="surge", surge_started_at=now_utc())
    return nid, g


async def candidates(
    db: Database,
    count: int,
    *,
    cosine: float = 0.9,
    text_: str = "Un fait nouveau",
    metrics: list[dict[str, int]] | None = None,
) -> list[int]:
    ids: list[int] = []
    for i in range(count):
        (cid,) = await bulk(db, None, 1, label=f"{text_} {i}", metrics=(metrics or [{}] * count)[i])
        await embed(db, cid, vec(cosine, axis=2 + i % 5))
        ids.append(cid)
    return ids


def rng() -> random.Random:
    return random.Random(11)  # noqa: S311 (tests: reproducible sampling)


async def sample(
    db: Database, settings: Settings, ids: list[int]
) -> tuple[list[int], dict[int, int]]:
    async with db.transaction() as s:
        return await filter_for_surges(s, settings, ids, rng=rng())


async def test_without_a_surging_narrative_nothing_is_sampled(db: Database) -> None:
    settings = make_settings(fc_sampling_per_minute=1)
    g = await seed(db, "Calm claim", vec(1.0))
    await build_narrative(db, g)  # active, not surging
    ids = await candidates(db, 6)
    assert await sample(db, settings, ids) == (ids, {})
    assert await sample(db, settings, []) == ([], {})


async def test_under_the_rate_limit_every_close_post_is_extracted(db: Database) -> None:
    await surging_narrative(db)
    ids = await candidates(db, 6)
    assert await sample(db, make_settings(fc_sampling_per_minute=200), ids) == (ids, {})


async def test_beyond_the_rate_limit_only_a_stratified_sample_is_extracted(db: Database) -> None:
    settings = make_settings(fc_sampling_per_minute=2, fc_novelty_budget_share=0.0)
    nid, _ = await surging_narrative(db)
    close = await candidates(db, 10, text_="Même histoire")
    far = await candidates(db, 3, cosine=0.5, text_="Autre sujet")  # not close to the medoid
    kept, sampled = await sample(db, settings, close + far)

    assert set(far) <= set(kept)  # far posts are never counted
    assert len([c for c in kept if c in close]) == 2  # round(10 * 2 / 10)
    assert set(sampled) == set(close) - set(kept)
    assert set(sampled.values()) == {nid}
    assert await sample(db, settings, close + far) == (kept, sampled)  # seeded: reproducible


async def test_the_sample_is_stratified_by_platform(db: Database) -> None:
    settings = make_settings(fc_sampling_per_minute=2, fc_novelty_budget_share=0.0)
    await surging_narrative(db)
    on_x = await candidates(db, 8, text_="Post X")
    bluesky: list[int] = []
    for i in range(2):
        (cid,) = await bulk(db, None, 1, platform="bluesky", label=f"Post bluesky {i}")
        await embed(db, cid, vec(0.9, axis=7))
        bluesky.append(cid)
    kept, sampled = await sample(db, settings, on_x + bluesky)
    assert len([c for c in kept if c in on_x]) == 2  # rate 10: 2/10 of the 8 X posts
    assert len([c for c in kept if c in bluesky]) == 1  # a small stratum keeps at least one
    assert len(sampled) == 7


async def test_the_rate_counts_what_was_extracted_in_the_last_minute(db: Database) -> None:
    settings = make_settings(fc_sampling_per_minute=8, fc_novelty_budget_share=0.0)
    await surging_narrative(db)
    earlier = await candidates(db, 5, text_="Déjà extrait")
    async with db.transaction() as s:
        for cid in earlier:
            await s.execute(
                text(
                    "INSERT INTO content_extractions (content_id, level, status) "
                    "VALUES (:c, 'N1', 'extracted')"
                ),
                {"c": cid},
            )
    fresh = await candidates(db, 6, text_="Nouveau lot")
    kept, sampled = await sample(db, settings, fresh)  # 5 + 6 = 11 per minute > 8
    assert len(kept) == round(6 * 8 / 11) == 4 and len(sampled) == 2
    old = now_utc() - timedelta(minutes=5)
    async with db.transaction() as s:
        await s.execute(text("UPDATE content_extractions SET created_at = :d"), {"d": old})
    assert await sample(db, settings, fresh) == (fresh, {})  # 6 per minute: under the limit


async def test_a_novelty_that_takes_forces_its_best_post_into_extraction(db: Database) -> None:
    settings = make_settings(
        fc_sampling_per_minute=1,
        fc_novelty_posts=3,
        fc_novelty_accounts=3,
        fc_novelty_budget_share=1.0,
    )
    nid, _ = await surging_narrative(db)
    metrics = [{"impressions": 100 + 50 * i} for i in range(12)]
    ids = await candidates(
        db, 12, text_="Le total atteint 42 000 euros selon le rapport numéro", metrics=metrics
    )
    kept, sampled = await sample(db, settings, ids)

    forced = [c for c in kept if c not in sampled]
    assert len(kept) == 1 + 1 and len(sampled) == 10  # one sampled post, one forced novelty
    row = (
        await rows(
            db,
            "SELECT posts, accounts, cardinality(account_ids), best_content_id, best_engagement, "
            "forced_at IS NOT NULL FROM narrative_novelty WHERE narrative_id = :n "
            "AND fingerprint = 'number:42000'",
            n=nid,
        )
    )[0]
    assert row[0] == 11 and row[1] == 10 and row[2] == 10  # accounts capped at 10
    assert row[3] in kept and row[5]
    sampled_out = set(sampled) | {row[3]}
    best_of_sampled = max(100 + 50 * ids.index(c) for c in sampled_out)
    assert row[4] == best_of_sampled  # most engaged post among the sampled-out ones
    assert len(forced) == 2 and row[3] in forced  # the sampled post and the forced one

    again_kept, again_sampled = await sample(db, settings, ids)
    assert row[3] in again_sampled or row[3] in again_kept  # a taken fingerprint is not re-forced
    assert (
        await scalar(
            db,
            "SELECT count(*) FROM narrative_novelty WHERE forced_at IS NOT NULL AND "
            "fingerprint = 'number:42000'",
        )
        == 1
    )


async def test_the_novelty_budget_limits_forced_extractions(db: Database) -> None:
    settings = make_settings(fc_sampling_per_minute=1, fc_novelty_budget_share=0.2)
    nid, _ = await surging_narrative(db)
    ids = await candidates(db, 12, text_="Le total atteint 42 000 euros selon le rapport numéro")
    kept, sampled = await sample(db, settings, ids)
    assert len(kept) == 1 and len(sampled) == 11  # floor(0.2 * 1 extraction) = 0 forced
    assert await scalar(
        db,
        "SELECT forced_at IS NULL FROM narrative_novelty WHERE narrative_id "
        "= :n AND fingerprint = 'number:42000'",
        n=nid,
    )


async def test_a_fingerprint_that_does_not_take_forces_nothing(db: Database) -> None:
    settings = make_settings(
        fc_sampling_per_minute=1, fc_novelty_posts=50, fc_novelty_budget_share=1.0
    )
    await surging_narrative(db)
    ids = await candidates(db, 12, text_="Le total atteint 42 000 euros selon le rapport numéro")
    kept, sampled = await sample(db, settings, ids)
    assert len(kept) == 1 and len(sampled) == 11


async def test_a_post_above_the_engagement_p90_is_forced(db: Database) -> None:
    settings = make_settings(
        fc_sampling_per_minute=1, fc_novelty_posts=50, fc_novelty_budget_share=1.0
    )
    _, g = await surging_narrative(db)
    await bulk(db, g, 12, metrics={"impressions": 100})  # the narrative's usual engagement
    ids = await candidates(
        db,
        3,
        text_="Un récit viral",
        metrics=[{"impressions": 50_000}] * 3,
    )
    kept, sampled = await sample(db, settings, ids)
    assert len(kept) == 2 and len(sampled) == 1  # one sampled post, one forced by engagement
    assert set(kept) | set(sampled) == set(ids)
