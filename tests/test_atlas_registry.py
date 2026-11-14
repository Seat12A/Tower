"""Editable registry (use cases), country registries, control seed job and seeds."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from sqlalchemy import select, text

from tower.core.db.database import Database
from tower.core.domain import error_codes as ec
from tower.core.domain.errors import Conflict, InvalidInput, NotFound, PermissionDenied
from tower.core.domain.security import Actor, Permission, Role
from tower.core.factcheck import names
from tower.core.models.atlas import (
    ElectionEntry,
    JurisdictionSource,
    LegalPortal,
    SourceRegistryVersion,
)
from tower.core.models.events import Event
from tower.core.models.factcheck import SourceRegistryEntry
from tower.core.usecases import registry as uc

SERVICE = Actor.service("intercom", frozenset(Permission))
REVIEWER = Actor.staff(1001, frozenset({Role.REVIEWER}), SERVICE)
EDITOR = Actor.staff(1002, frozenset({Role.EDITOR}), SERVICE)


@pytest.fixture(autouse=True)
async def _clean_country_tables(db: Database) -> None:
    async with db.transaction() as s:
        await s.execute(
            text("TRUNCATE jurisdiction_sources, legal_portals, election_calendar, jobs CASCADE")
        )


def entry_args(**over: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "domain": "https://www.Example.org:8080/path",
        "publisher": "Example",
        "tier": "secondary",
        "kind": "press",
        "country": "FR",
        "reason": "Public correction policy",
    }
    args.update(over)
    return args


async def make(db: Database, **over: Any) -> SourceRegistryEntry:
    async with db.transaction() as s:
        return await uc.create_entry(s, EDITOR, **entry_args(**over))


async def events(db: Database, topic: str) -> list[Event]:
    async with db.session() as s:
        return list(await s.scalars(select(Event).where(Event.topic == topic).order_by(Event.id)))


async def test_permissions(db: Database) -> None:
    async with db.transaction() as s:
        with pytest.raises(PermissionDenied):
            await uc.create_entry(s, REVIEWER, **entry_args())
    entry = await make(db)
    async with db.session() as s:
        assert (await uc.get_entry(s, REVIEWER, "example.org")).id == entry.id
        assert len(await uc.list_registry(s, REVIEWER)) == 1
        with pytest.raises(PermissionDenied):
            await uc.get_entry(s, Actor.anonymous(), "example.org")
    async with db.transaction() as s:
        with pytest.raises(PermissionDenied):
            await uc.update_entry(
                s, REVIEWER, entry.id, expected_version=1, changes={"tier": "primary"}, reason="x"
            )
        with pytest.raises(PermissionDenied):
            await uc.request_control_seed(s, REVIEWER, entry.id)


async def test_create_normalizes_and_versions(db: Database) -> None:
    entry = await make(db)
    assert entry.domain == "example.org"
    assert entry.version == 1
    assert entry.origin == "team"
    async with db.session() as s:
        history = await uc.entry_history(s, REVIEWER, "example.org")
    assert [(h.version, h.change) for h in history] == [(1, "created")]
    assert history[0].snapshot["domain"] == "example.org"
    assert history[0].changed_by == EDITOR.label


@pytest.mark.parametrize(
    ("over", "field"),
    [
        ({"domain": "localhost"}, "domain"),
        ({"tier": "gold"}, "tier"),
        ({"kind": "blog"}, "kind"),
        ({"country": "fr"}, "country"),
        ({"reason": "  "}, "reason"),
        ({"qid": "12"}, "qid"),
        ({"statutory_independence": True}, "independence_basis"),
        ({"statutory_independence": True, "independence_basis": "vibes"}, "independence_basis"),
    ],
)
async def test_create_validation(db: Database, over: dict[str, Any], field: str) -> None:
    async with db.transaction() as s:
        with pytest.raises(InvalidInput) as exc:
            await uc.create_entry(s, EDITOR, **entry_args(**over))
    assert exc.value.code == ec.ATL_REGISTRY_INVALID.code
    assert exc.value.details.get("field") == field


async def test_create_duplicate_conflicts(db: Database) -> None:
    await make(db)
    async with db.transaction() as s:
        with pytest.raises(Conflict) as exc:
            await uc.create_entry(s, EDITOR, **entry_args(domain="example.org"))
    assert exc.value.code == ec.ATL_REGISTRY_CONFLICT.code


async def test_update_versions_history_and_lock(db: Database) -> None:
    entry = await make(db)
    async with db.transaction() as s:
        done = await uc.update_entry(
            s,
            EDITOR,
            entry.id,
            expected_version=1,
            changes={"tier": "primary"},
            reason="Official producer",
        )
    assert (done.version, done.tier, done.reason) == (2, "primary", "Official producer")
    async with db.transaction() as s:
        with pytest.raises(Conflict) as exc:
            await uc.update_entry(
                s, EDITOR, entry.id, expected_version=1, changes={"tier": "avoid"}, reason="x"
            )
    assert exc.value.code == ec.ATL_REGISTRY_CONFLICT.code
    async with db.session() as s:
        history = await uc.entry_history(s, EDITOR, str(entry.id))
    assert [h.version for h in history] == [2, 1]
    assert history[0].snapshot["tier"] == "primary"
    assert history[1].snapshot["tier"] == "secondary"


async def test_update_requires_reason_and_known_fields(db: Database) -> None:
    entry = await make(db)
    async with db.transaction() as s:
        with pytest.raises(InvalidInput):
            await uc.update_entry(
                s, EDITOR, entry.id, expected_version=1, changes={"tier": "avoid"}, reason=""
            )
        with pytest.raises(InvalidInput):
            await uc.update_entry(
                s, EDITOR, entry.id, expected_version=1, changes={"version": 9}, reason="x"
            )
        with pytest.raises(NotFound) as exc:
            await uc.update_entry(s, EDITOR, 9999, expected_version=1, changes={}, reason="x")
    assert exc.value.code == ec.ATL_REGISTRY_NOT_FOUND.code


async def test_independence_needs_basis_on_update(db: Database) -> None:
    entry = await make(db)
    async with db.transaction() as s:
        with pytest.raises(InvalidInput):
            await uc.update_entry(
                s,
                EDITOR,
                entry.id,
                expected_version=1,
                changes={"statutory_independence": True},
                reason="x",
            )
    async with db.transaction() as s:
        done = await uc.update_entry(
            s,
            EDITOR,
            entry.id,
            expected_version=1,
            changes={"statutory_independence": True, "independence_basis": "imf_sdds"},
            reason="IMF SDDS subscriber",
        )
    assert done.statutory_independence is True


async def test_human_control_is_team_origin(db: Database) -> None:
    entry = await make(db)
    async with db.transaction() as s:
        done = await uc.update_entry(
            s,
            EDITOR,
            entry.id,
            expected_version=1,
            changes={"controlled_by": "Some Group"},
            reason="Annual report",
        )
    assert (done.controlled_by, done.control_origin) == ("Some Group", "team")


async def test_deactivate_and_export(db: Database) -> None:
    keep = await make(db, domain="keep.org")
    gone = await make(db, domain="gone.org")
    async with db.transaction() as s:
        done = await uc.deactivate_entry(s, EDITOR, gone.id, expected_version=1, reason="Defunct")
        assert done.active is False
        with pytest.raises(InvalidInput):
            await uc.deactivate_entry(s, EDITOR, keep.id, expected_version=1, reason="")
    async with db.session() as s:
        exported = await uc.export_registry(s, REVIEWER)
        listed = await uc.list_registry(s, REVIEWER, active=None)
        history = await uc.entry_history(s, REVIEWER, gone.id)
    assert [e["domain"] for e in exported["entries"]] == ["keep.org"]
    assert exported["count"] == 1 and len(exported["digest"]) == 64
    assert len(listed) == 2
    assert history[0].change == "deactivated"


async def test_event_in_same_transaction(db: Database) -> None:
    async with db.transaction() as s:
        entry = await uc.create_entry(s, EDITOR, **entry_args())
        xid = str((await s.execute(text("SELECT pg_current_xact_id()::text"))).scalar_one())
    async with db.session() as s:
        evt_xid = (
            await s.execute(
                text("SELECT xid::text FROM events WHERE topic = :t"),
                {"t": names.SOURCE_REGISTRY_CHANGED},
            )
        ).scalar_one()
    assert evt_xid == xid
    (event,) = await events(db, names.SOURCE_REGISTRY_CHANGED)
    assert event.payload["registry_id"] == str(entry.id)
    assert event.payload["change"] == "created"
    assert event.payload["domain"] == "example.org"
    assert event.payload["changed_by"] == EDITOR.label


async def test_rolled_back_write_leaves_no_event(db: Database) -> None:
    with pytest.raises(RuntimeError):
        async with db.transaction() as s:
            await uc.create_entry(s, EDITOR, **entry_args())
            raise RuntimeError("boom")
    assert await events(db, names.SOURCE_REGISTRY_CHANGED) == []
    async with db.session() as s:
        assert await uc.list_registry(s, EDITOR) == []


async def test_control_seed_never_overwrites_team(db: Database) -> None:
    system = Actor.system("atlas")
    seeded = await make(db, domain="a.org")
    corrected = await make(db, domain="b.org", controlled_by="Team Owner")
    async with db.transaction() as s:
        assert await uc.apply_control_seed(
            s, system, seeded.id, controlled_by="Parent", controlled_by_qid="Q1", aliases=["P"]
        )
        assert not await uc.apply_control_seed(
            s, system, corrected.id, controlled_by="Parent", controlled_by_qid="Q1", aliases=[]
        )
        # seeding again with the same value is a no-op (no version bump)
        assert not await uc.apply_control_seed(
            s, system, seeded.id, controlled_by="Parent", controlled_by_qid="Q1", aliases=["P"]
        )
    async with db.session() as s:
        a = await uc.get_entry(s, EDITOR, "a.org")
        b = await uc.get_entry(s, EDITOR, "b.org")
    assert (a.controlled_by, a.control_origin, a.version, a.controlled_by_aliases) == (
        "Parent",
        "wikidata",
        2,
        ["P"],
    )
    assert (b.controlled_by, b.control_origin, b.version) == ("Team Owner", "team", 1)


async def test_team_clearing_blocks_the_seed(db: Database) -> None:
    system = Actor.system("atlas")
    entry = await make(db, domain="c.org")
    async with db.transaction() as s:
        await uc.update_entry(
            s, EDITOR, entry.id, expected_version=1, changes={"controlled_by": None}, reason="None"
        )
        assert not await uc.apply_control_seed(
            s, system, entry.id, controlled_by="Parent", controlled_by_qid=None, aliases=[]
        )


async def test_request_control_seed_dedupes(db: Database) -> None:
    entry = await make(db)
    async with db.transaction() as s:
        assert await uc.request_control_seed(s, EDITOR, "example.org") is True
        assert await uc.request_control_seed(s, EDITOR, str(entry.id)) is False
    async with db.session() as s:
        rows = (
            await s.execute(
                text("SELECT payload, dedupe_key, queue FROM jobs WHERE kind = :k"),
                {"k": names.JOB_CONTROL_SEED},
            )
        ).all()
    assert len(rows) == 1
    assert rows[0][0] == {"registry_id": str(entry.id)}
    assert rows[0][1] == names.control_seed_key(entry.id)
    assert rows[0][2] == names.ATLAS_QUEUE
    assert await events(db, names.SOURCE_REGISTRY_CHANGED) != []  # only the creation


# --- country registries -----------------------------------------------------------------


async def test_country_upsert_versions_and_event(db: Database) -> None:
    kwargs: dict[str, Any] = {
        "country": "FR",
        "source_type": "statistics",
        "key": "insee",
        "name": "INSEE",
        "domain": "https://www.insee.fr/",
    }
    async with db.transaction() as s:
        row = await uc.upsert_jurisdiction_source(s, EDITOR, **kwargs)
    assert (row.version, row.domain, row.updated_by) == (1, "insee.fr", EDITOR.label)
    async with db.transaction() as s:
        same = await uc.upsert_jurisdiction_source(s, EDITOR, **kwargs)
        assert same.version == 1  # unchanged: no bump
        changed = await uc.upsert_jurisdiction_source(s, EDITOR, **{**kwargs, "name": "Insee"})
        assert changed.version == 2
        with pytest.raises(Conflict):
            await uc.upsert_jurisdiction_source(s, EDITOR, **kwargs, expected_version=1)
    evts = await events(db, names.COUNTRY_REGISTRY_CHANGED)
    assert [e.payload["change"] for e in evts] == ["created", "updated"]
    assert evts[0].payload["table"] == "jurisdiction_sources"
    assert evts[0].payload["country"] == "FR"


async def test_country_validation(db: Database) -> None:
    async with db.transaction() as s:
        with pytest.raises(PermissionDenied):
            await uc.upsert_legal_portal(
                s, REVIEWER, country="FR", name="x", url="https://x.fr/", access="web"
            )
        with pytest.raises(InvalidInput):
            await uc.upsert_jurisdiction_source(
                s, EDITOR, country="FR", source_type="statistics", key="k", name="n"
            )
        with pytest.raises(InvalidInput):
            await uc.upsert_legal_portal(
                s, EDITOR, country="XX1", name="x", url="https://x.fr/", access="web"
            )
        with pytest.raises(InvalidInput):
            await uc.upsert_legal_portal(
                s, EDITOR, country="FR", name="x", url="https://x.fr/", access="magic"
            )
        with pytest.raises(InvalidInput):
            await uc.upsert_election(
                s,
                EDITOR,
                country="FR",
                kind="presidential",
                name="n",
                first_round=date(2027, 4, 24),
                second_round=date(2027, 4, 10),
            )


async def test_portal_and_election_roundtrip(db: Database) -> None:
    async with db.transaction() as s:
        portal = await uc.upsert_legal_portal(
            s,
            EDITOR,
            country="EU",
            name="EUR-Lex",
            url="https://eur-lex.europa.eu/",
            access="api",
            eli=True,
        )
        election = await uc.upsert_election(
            s, EDITOR, country="FR", kind="local", name="Test", first_round=date(2030, 3, 1)
        )
    assert portal.domain == "eur-lex.europa.eu" and election.version == 1
    async with db.session() as s:
        assert [p.name for p in await uc.list_legal_portals(s, REVIEWER, "EU")] == ["EUR-Lex"]
        assert len(await uc.list_elections(s, REVIEWER, "FR", upcoming_from=date(2030, 1, 1))) == 1
        assert await uc.list_elections(s, REVIEWER, "FR", upcoming_from=date(2031, 1, 1)) == []
        assert await uc.list_jurisdiction_sources(s, REVIEWER, "FR") == []


# --- seeds ------------------------------------------------------------------------------


async def test_seed_is_idempotent_and_respects_human_edits(db: Database) -> None:
    from tower.core.atlas_seed_registries import LEGAL_PORTALS, SOURCES, seed_country_registries

    async with db.transaction() as s:
        first = await seed_country_registries(s)
    assert first["jurisdiction_sources"] == len(SOURCES)
    assert first["legal_portals"] == len(LEGAL_PORTALS)
    async with db.transaction() as s:
        assert sum((await seed_country_registries(s)).values()) == 0
    async with db.transaction() as s:
        await uc.upsert_jurisdiction_source(
            s,
            EDITOR,
            country="FR",
            source_type="statistics",
            key="insee",
            name="Edited by a human",
            adapter="insee",
            domain="insee.fr",
        )
        # an untouched seed row drifts: the seed refreshes it
        await s.execute(
            text("UPDATE legal_portals SET name = 'drift' WHERE access = 'bulk' AND country = 'FR'")
        )
    async with db.transaction() as s:
        again = await seed_country_registries(s)
    assert again["legal_portals"] == 1
    assert again["jurisdiction_sources"] == 0
    async with db.session() as s:
        insee = await s.scalar(select(JurisdictionSource).where(JurisdictionSource.key == "insee"))
        fr_portal = await s.scalar(select(LegalPortal).where(LegalPortal.country == "FR"))
        assert (await s.scalars(select(ElectionEntry))).all() == []
    assert insee is not None and insee.name == "Edited by a human"
    assert fr_portal is not None and fr_portal.name == "Légifrance" and fr_portal.version == 2


# --- control seed job ---------------------------------------------------------------------


def job_ctx(db: Database, settings: Any, registry_id: int) -> Any:
    import structlog

    from tower.core.jobs.queue import JobRecord
    from tower.core.services.base import JobContext

    record = JobRecord(
        id=1,
        queue="atlas",
        kind=names.JOB_CONTROL_SEED,
        payload={"registry_id": str(registry_id)},
        priority=0,
        attempts=1,
        max_attempts=5,
        dedupe_key=None,
        created_at=None,  # type: ignore[arg-type]
    )
    return JobContext(job=record, db=db, settings=settings, log=structlog.get_logger())


def test_pick_controller_prefers_parent_and_ignores_ended() -> None:
    from tower.services.atlas.control_seed import Relation, pick_controller

    owner = Relation("P127", "Q1", ("Propriétaire", "Owner"))
    parent = Relation("P749", "Q2", ("Maison mère", "Parent"))
    old_parent = Relation("P749", "Q3", ("Ancienne mère",), ended=True)
    chosen, aliases = pick_controller([owner, old_parent, parent])
    assert chosen is parent
    assert set(aliases) == {"Parent", "Propriétaire", "Owner", "Ancienne mère"}
    chosen, _ = pick_controller([old_parent, owner])
    assert chosen is owner
    assert pick_controller([old_parent]) == (None, [])


async def test_control_seed_job(db: Database, settings: Any) -> None:
    from tower.services.atlas.control_seed import Relation, control_seed

    entry = await make(db, qid="Q42")

    async def fake(qid: str) -> list[Relation]:
        assert qid == "Q42"
        return [
            Relation("P127", "Q1", ("Etat", "State")),
            Relation("P749", "Q2", ("Groupe Mère", "Parent Group")),
            Relation("P749", "Q3", ("Ancien parent",), ended=True),
        ]

    await control_seed(job_ctx(db, settings, entry.id), fetch=fake)
    async with db.session() as s:
        row = await uc.get_entry(s, EDITOR, entry.id)
    assert (row.controlled_by, row.controlled_by_qid, row.control_origin) == (
        "Groupe Mère",
        "Q2",
        "wikidata",
    )
    assert "Etat" in row.controlled_by_aliases and "Parent Group" in row.controlled_by_aliases
    assert row.updated_by == "system:atlas"
    async with db.session() as s:
        versions = (
            await s.scalars(select(SourceRegistryVersion.version).order_by("version"))
        ).all()
    assert list(versions) == [1, 2]


async def test_control_seed_failure_retries_later(db: Database, settings: Any) -> None:
    from tower.core.jobs.queue import RetryLater
    from tower.core.ports.knowledge import SourceUnavailable
    from tower.services.atlas.control_seed import Relation, control_seed

    entry = await make(db, qid="Q42")

    async def boom(qid: str) -> list[Relation]:
        raise SourceUnavailable("down", code=ec.ATL_SOURCE_UNAVAILABLE, source="wikidata")

    with pytest.raises(RetryLater) as exc:
        await control_seed(job_ctx(db, settings, entry.id), fetch=boom)
    assert exc.value.delay == 3600


async def test_control_seed_skips_without_qid_and_enqueue_periodic(
    db: Database, settings: Any
) -> None:
    from tower.services.atlas.control_seed import control_seed, enqueue_control_seeds

    no_qid = await make(db, domain="noqid.org")
    with_qid = await make(db, domain="qid.org", qid="Q5")
    team = await make(db, domain="team.org", qid="Q6", controlled_by="X")
    await control_seed(job_ctx(db, settings, no_qid.id))  # nothing to fetch, no network
    async with db.transaction() as s:
        assert await enqueue_control_seeds(s, settings) == 1
    async with db.transaction() as s:
        assert await enqueue_control_seeds(s, settings) == 0  # already queued
    async with db.session() as s:
        payloads = (
            (
                await s.execute(
                    text("SELECT payload FROM jobs WHERE kind = :k"), {"k": names.JOB_CONTROL_SEED}
                )
            )
            .scalars()
            .all()
        )
    assert payloads == [{"registry_id": str(with_qid.id)}]
    assert team.id != with_qid.id
