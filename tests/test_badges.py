"""Verification badges: use cases, gate endpoints, service jobs (needs PostgreSQL)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import structlog
from sqlalchemy import select, text

from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain import error_codes as ec
from tower.core.domain.badges import AccountRef, BadgeCategory, BadgeSource
from tower.core.domain.errors import Conflict, DomainError, PermissionDenied
from tower.core.domain.platforms import Platform
from tower.core.domain.security import Actor, Permission
from tower.core.jobs.queue import JobRecord
from tower.core.models.badges import Badge, BadgeAffiliation, BadgeEvent
from tower.core.ports.badges import WikidataAccount, WikidataItem
from tower.core.ports.platforms import PublicAccount
from tower.core.services.base import JobContext
from tower.core.usecases import badge_affiliations as aff
from tower.core.usecases import badge_sources as sources
from tower.core.usecases import badge_submissions as sub
from tower.core.usecases import badges as uc

from .conftest import TokenFactory, make_settings

SYSTEM = Actor.system("test")
STAFF = Actor.service("bot", frozenset(Permission))
ANON = Actor.anonymous()


def ref(xid: str) -> AccountRef:
    return AccountRef(Platform.X, xid)


async def topics(db: Database) -> list[str]:
    async with db.session() as s:
        return list((await s.scalars(text("SELECT topic FROM events ORDER BY id"))).all())


async def actions(db: Database, xid: str) -> list[str]:
    async with db.session() as s:
        rows = await s.scalars(
            text(
                "SELECT e.action FROM badge_events e JOIN platform_accounts a "
                "ON a.id = e.account_id WHERE a.external_id = :x ORDER BY e.id"
            ),
            {"x": xid},
        )
        return list(rows.all())


async def org_with_member(db: Database) -> str:
    """Media org 100, member 200 affiliated (active). Returns the affiliation id."""
    async with db.transaction() as s:
        await uc.grant_badge(s, STAFF, ref("100"), category="media", label="Le Journal")
        a = await aff.create_affiliation(s, STAFF, ref("200"), org_ref=ref("100"), title="Reporter")
        return str(a.public_id)


# --- Badges -----------------------------------------------------------------------------


async def test_grant_lookup_and_history(db: Database) -> None:
    async with db.transaction() as s:
        badge, account = await uc.grant_badge(
            s, STAFF, ref("1"), category="expert", label="Dr A", handle="dra", note="checked"
        )
        assert badge.status == "active" and badge.source == "manual" and badge.version == 2
        assert account.handle == "dra"
    async with db.session() as s:
        found = await uc.lookup_badges(s, ANON, "x", ["1", "2"])
    assert found == {
        "1": {
            "category": "expert",
            "color": "#22D3EE",
            "badge": {"category": "expert", "color": "#22D3EE", "label": "Dr A"},
            "affiliation": None,
        }
    }
    assert await actions(db, "1") == ["granted"]
    assert await topics(db) == ["badge.changed"]
    # Same values: no new version, no event
    async with db.transaction() as s:
        badge, _ = await uc.grant_badge(s, STAFF, ref("1"), category="expert")
        assert badge.version == 2
    # Optimistic lock
    async with db.transaction() as s:
        with pytest.raises(Conflict) as exc:
            await uc.grant_badge(s, STAFF, ref("1"), category="media", version=1)
        assert exc.value.error_code is ec.BDG_VERSION_CONFLICT


async def test_permissions_are_checked_in_use_cases(db: Database) -> None:
    async with db.transaction() as s:
        with pytest.raises(PermissionDenied):
            await uc.grant_badge(s, ANON, ref("1"), category="expert")
        with pytest.raises(PermissionDenied):
            await sub.list_reports(s, ANON)


async def test_revoke_is_sticky_against_automatic_sources(db: Database) -> None:
    async with db.transaction() as s:
        await uc.grant_badge(s, STAFF, ref("5"), category="personality")
        await uc.revoke_badge(s, STAFF, ref("5"), reason="error")
        account = await uc.find_account(s, ref("5"))
        assert account is not None
        grant = uc.AutoGrant(category=BadgeCategory.PERSONALITY, source=BadgeSource.WIKIDATA)
        assert await uc.apply_automatic(s, SYSTEM, account, grant) is None
    async with db.session() as s:
        assert await uc.lookup_badges(s, ANON, "x", ["5"]) == {}
    assert await actions(db, "5") == ["granted", "revoked"]


async def test_block_beats_sources_and_requests(db: Database) -> None:
    affiliation = await org_with_member(db)
    async with db.transaction() as s:
        await uc.block_account(s, STAFF, ref("200"), reason="impersonation")
    async with db.session() as s:
        assert await uc.lookup_badges(s, ANON, "x", ["200"]) == {}
        a = await aff.get_affiliation(s, STAFF, affiliation)
        assert a["status"] == "revoked" and a["end_reason"] == "account_blocked"
    async with db.transaction() as s:
        account = await uc.find_account(s, ref("200"))
        assert account is not None
        grant = uc.AutoGrant(category=BadgeCategory.INSTITUTIONAL, source=BadgeSource.X_GOVERNMENT)
        assert await uc.apply_automatic(s, SYSTEM, account, grant) is None
        with pytest.raises(Conflict) as exc:
            await uc.grant_badge(s, STAFF, ref("200"), category="expert")
        assert exc.value.error_code is ec.BDG_ACCOUNT_BLOCKED
    async with db.transaction() as s:
        with pytest.raises(Conflict):
            await sub.submit_request(s, ANON, kind="badge", account=ref("200"), category="expert")
    async with db.transaction() as s:
        await uc.unblock_account(s, STAFF, ref("200"))
        badge, _ = await uc.grant_badge(s, STAFF, ref("200"), category="expert")
        assert badge.status == "active"
    assert (await actions(db, "200"))[-4:] == [
        "blocked",
        "affiliation_revoked",
        "unblocked",
        "granted",
    ]


async def test_automatic_source_rules(db: Database) -> None:
    async with db.transaction() as s:
        account = await uc.ensure_account(s, ref("7"))
        wd = uc.AutoGrant(
            category=BadgeCategory.MEDIA, source=BadgeSource.WIKIDATA, wikidata_qid="Q7"
        )
        assert (await uc.apply_automatic(s, SYSTEM, account, wd)).value == "granted"  # type: ignore[union-attr]
        # X government outranks a Wikidata badge
        xg = uc.AutoGrant(category=BadgeCategory.INSTITUTIONAL, source=BadgeSource.X_GOVERNMENT)
        assert (await uc.apply_automatic(s, SYSTEM, account, xg)).value == "updated"  # type: ignore[union-attr]
        # Wikidata no longer changes an X government badge
        assert await uc.apply_automatic(s, SYSTEM, account, wd) is None
        # Another QID claiming the same account is ignored (safety)
        other = uc.AutoGrant(
            category=BadgeCategory.PERSONALITY, source=BadgeSource.WIKIDATA, wikidata_qid="Q8"
        )
        assert await uc.apply_automatic(s, SYSTEM, account, other) is None
        badge = await s.scalar(select(Badge).where(Badge.account_id == account.id))
        assert badge is not None
        assert (badge.category, badge.source, badge.wikidata_qid) == (
            "institutional",
            "x_government",
            "Q7",
        )
        # A team category decision makes it manual
        await uc.grant_badge(s, STAFF, ref("7"), category="media")
        assert await uc.apply_automatic(s, SYSTEM, account, xg) is None
        await s.refresh(badge)
        assert (badge.category, badge.source) == ("media", "manual")


# --- Affiliations --------------------------------------------------------------------------


async def test_affiliation_display_and_primary(db: Database) -> None:
    first = await org_with_member(db)
    async with db.session() as s:
        found = await uc.lookup_badges(s, ANON, "x", ["200", "100"])
    assert found["200"]["category"] == "media" and found["200"]["badge"] is None
    assert found["200"]["affiliation"]["org"]["label"] == "Le Journal"
    assert found["200"]["affiliation"]["title"] == "Reporter"
    # Own badge wins over the organisation colour, affiliation still shown
    async with db.transaction() as s:
        await uc.grant_badge(s, STAFF, ref("200"), category="personality")
        await uc.grant_badge(s, STAFF, ref("101"), category="institutional", label="Ville")
        second = await aff.create_affiliation(s, STAFF, ref("200"), org_ref=ref("101"))
        assert second.is_primary is False
    async with db.session() as s:
        found = await uc.lookup_badges(s, ANON, "x", ["200"])
    assert found["200"]["color"] == "#3B5BFF"
    assert found["200"]["affiliation"]["org"]["external_id"] == "100"
    async with db.transaction() as s:
        await aff.set_primary_affiliation(s, STAFF, str(second.public_id))
    async with db.session() as s:
        found = await uc.lookup_badges(s, ANON, "x", ["200"])
        details = await uc.account_badge_details(s, ANON, ref("200"))
    assert found["200"]["affiliation"]["org"]["external_id"] == "101"
    assert [a["primary"] for a in details["affiliations"]] == [True, False]
    # Revoking the primary promotes the remaining one
    async with db.transaction() as s:
        await aff.revoke_affiliation(s, STAFF, str(second.public_id), reason="left")
    async with db.session() as s:
        found = await uc.lookup_badges(s, ANON, "x", ["200"])
        assert found["200"]["affiliation"]["org"]["external_id"] == "100"
        assert (await aff.get_affiliation(s, STAFF, first))["primary"] is True


async def test_affiliation_rules(db: Database) -> None:
    await org_with_member(db)
    async with db.transaction() as s:
        await uc.grant_badge(s, STAFF, ref("300"), category="expert")
    async with db.transaction() as s:
        with pytest.raises(DomainError) as exc:
            await aff.create_affiliation(s, STAFF, ref("201"), org_ref=ref("300"))
        assert exc.value.error_code is ec.BDG_NOT_AFFILIABLE
    async with db.transaction() as s:
        with pytest.raises(DomainError) as exc:
            await aff.create_affiliation(s, STAFF, ref("100"), org_ref=ref("100"))
        assert exc.value.error_code is ec.BDG_SELF_AFFILIATION
    async with db.transaction() as s:
        with pytest.raises(DomainError) as exc:
            await aff.create_affiliation(s, STAFF, ref("200"), org_ref=ref("100"))
        assert exc.value.error_code is ec.BDG_AFFILIATION_EXISTS
    # Pending (team, not validated) → not displayed until validated
    async with db.transaction() as s:
        pending = await aff.create_affiliation(
            s, STAFF, ref("202"), org_ref=ref("100"), validate=False
        )
        assert pending.status == "pending"
    async with db.session() as s:
        assert await uc.lookup_badges(s, ANON, "x", ["202"]) == {}
    async with db.transaction() as s:
        with pytest.raises(DomainError) as exc:
            await aff.revoke_affiliation(s, STAFF, str(pending.public_id))
        assert exc.value.error_code is ec.BDG_AFFILIATION_TRANSITION
        a = await aff.validate_affiliation(s, STAFF, str(pending.public_id))
        assert a.status == "active" and a.expires_at is not None
        assert a.expires_at - a.validated_at == timedelta(days=365)  # type: ignore[operator]


async def test_org_losing_badge_ends_all_affiliations(db: Database) -> None:
    first = await org_with_member(db)
    async with db.transaction() as s:
        pending = await aff.create_affiliation(
            s, STAFF, ref("203"), org_ref=ref("100"), validate=False
        )
        await uc.revoke_badge(s, STAFF, ref("100"), reason="closed")
    async with db.session() as s:
        assert (await aff.get_affiliation(s, STAFF, first))["status"] == "revoked"
        assert (await aff.get_affiliation(s, STAFF, str(pending.public_id)))["status"] == "rejected"
        assert await uc.lookup_badges(s, ANON, "x", ["200"]) == {}
    assert "badge.affiliation_changed" in await topics(db)


async def test_org_category_change_ends_affiliations(db: Database) -> None:
    first = await org_with_member(db)
    async with db.transaction() as s:
        await uc.grant_badge(s, STAFF, ref("100"), category="personality")
    async with db.session() as s:
        a = await aff.get_affiliation(s, STAFF, first)
    assert a["status"] == "revoked" and a["end_reason"] == "org_category_changed"


async def test_expiry_and_reconfirmation(db: Database) -> None:
    first = await org_with_member(db)
    async with db.transaction() as s:
        a = await aff.reconfirm_affiliation(s, STAFF, first)
        assert a.expires_at is not None and a.expires_at > datetime.now(UTC) + timedelta(days=364)
    later = datetime.now(UTC) + timedelta(days=366)
    async with db.transaction() as s:
        assert await aff.expire_affiliations(s, SYSTEM, now=later) == 1
    async with db.session() as s:
        got = await aff.get_affiliation(s, STAFF, first)
    assert got["status"] == "expired" and got["primary"] is False
    async with db.transaction() as s:
        again = await aff.validate_affiliation(s, STAFF, first)
        assert again.status == "active" and again.is_primary
    assert (await actions(db, "200"))[-2:] == ["affiliation_expired", "affiliation_validated"]


async def test_lookup_hides_expired_before_the_job(db: Database) -> None:
    first = await org_with_member(db)
    async with db.transaction() as s:
        await s.execute(text("UPDATE badge_affiliations SET expires_at = now() - interval '1 day'"))
    async with db.session() as s:
        assert await uc.lookup_badges(s, ANON, "x", ["200"]) == {}
        assert (await aff.get_affiliation(s, STAFF, first))["status"] == "active"


# --- Requests and reports -------------------------------------------------------------


async def test_badge_request_flow(db: Database) -> None:
    async with db.transaction() as s:
        r = await sub.submit_request(
            s,
            ANON,
            kind="badge",
            account=ref("400"),
            category="expert",
            justification="Researcher at X",
            contact="me@example.org",
            evidence_urls=["https://example.org/p"],
        )
    async with db.transaction() as s:
        with pytest.raises(Conflict) as exc:
            await sub.submit_request(s, ANON, kind="badge", account=ref("400"), category="media")
        assert exc.value.error_code is ec.BDG_REQUEST_DUPLICATE
    async with db.session() as s:
        listed = await sub.list_requests(s, STAFF)
    assert listed[0]["contact"] == "me@example.org" and listed[0]["submitted_by"] == "anonymous"
    async with db.transaction() as s:
        await sub.approve_request(s, STAFF, str(r.public_id), category="media")
    async with db.transaction() as s:
        with pytest.raises(Conflict) as exc:
            await sub.reject_request(s, STAFF, str(r.public_id))
        assert exc.value.error_code is ec.BDG_REQUEST_DECIDED
    async with db.session() as s:
        b, _ = await uc.get_badge(s, STAFF, ref("400"))
    assert (b.category, b.source) == ("media", "request")
    published = await topics(db)
    assert "badge.request_submitted" in published and "badge.request_decided" in published
    async with db.session() as s:
        payloads = (await s.scalars(text("SELECT payload::text FROM events"))).all()
    assert all("me@example.org" not in p for p in payloads)


async def test_affiliation_request_flow(db: Database) -> None:
    async with db.transaction() as s:
        await uc.grant_badge(s, STAFF, ref("100"), category="organization")
        r = await sub.submit_request(
            s, ANON, kind="affiliation", account=ref("500"), org=ref("100"), title="CEO"
        )
        await sub.approve_request(s, STAFF, str(r.public_id))
    async with db.session() as s:
        rows = await aff.list_affiliations(s, STAFF, member_ref=ref("500"))
    assert rows[0]["status"] == "active" and rows[0]["source"] == "request"
    assert rows[0]["title"] == "CEO"


async def test_reports(db: Database) -> None:
    async with db.transaction() as s:
        with pytest.raises(DomainError) as exc:
            await sub.submit_report(s, ANON, account=ref("600"), reason="impersonation")
        assert exc.value.error_code is ec.BDG_NOT_FOUND
        await uc.grant_badge(s, STAFF, ref("600"), category="personality")
        r = await sub.submit_report(s, ANON, account=ref("600"), reason="wrong_category")
    async with db.transaction() as s:
        done = await sub.resolve_report(s, STAFF, str(r.public_id), status="dismissed")
        assert done.status == "dismissed"
        with pytest.raises(Conflict):
            await sub.resolve_report(s, STAFF, str(r.public_id), status="resolved")


# --- Sources ----------------------------------------------------------------------------


async def test_wikidata_batch(db: Database) -> None:
    items = {
        "Q10": WikidataItem("Q10", "Parti A", types=("Q7278",), logo="Logo A.svg"),
        "Q11": WikidataItem(
            "Q11", "Élu B", types=("Q5",), occupations=("Q82955",), links=(("P102", "Q10"),)
        ),
        "Q12": WikidataItem(
            "Q12", "Joueur C", types=("Q5",), occupations=("Q937857",), links=(("P54", "Q99"),)
        ),
        "Q13": WikidataItem("Q13", "Inconnu", types=("Q5",), occupations=("Q1",)),
    }
    triples = [["Q11", "11", "elub"], ["Q12", "12", ""], ["Q13", "13", ""]]
    async with db.transaction() as s:
        stats = await sources.apply_wikidata_batch(s, SYSTEM, triples, items)
    assert stats["granted"] == 2 and stats["unclassified"] == 1 and stats["proposed"] == 0
    # The party arrives in a later batch: the stored link proposes the affiliation
    async with db.transaction() as s:
        stats = await sources.apply_wikidata_batch(s, SYSTEM, [["Q10", "10", "partia"]], items)
    assert stats == {"granted": 1, "updated": 0, "unchanged": 0, "unclassified": 0, "proposed": 1}
    async with db.session() as s:
        rows = await aff.list_affiliations(s, STAFF, status="pending")
        org_badge, _ = await uc.get_badge(s, STAFF, ref("10"))
    assert rows[0]["member"]["external_id"] == "11" and rows[0]["wikidata_property"] == "P102"
    assert org_badge.logo_url and org_badge.logo_url.endswith("Logo_A.svg")
    # A rejected Wikidata affiliation is never proposed again
    async with db.transaction() as s:
        await aff.reject_affiliation(s, STAFF, rows[0]["public_id"])
        stats = await sources.apply_wikidata_batch(s, SYSTEM, triples, items)
    assert stats["proposed"] == 0 and stats["unchanged"] == 2
    assert "affiliation_created" in await actions(db, "11")


async def test_stale_wikidata_revocation_guard(db: Database) -> None:
    items = {"Q11": WikidataItem("Q11", "B", types=("Q5",), occupations=("Q82955",))}
    async with db.transaction() as s:
        await sources.apply_wikidata_batch(s, SYSTEM, [["Q11", "11", ""]], items)
        await s.execute(text("UPDATE badges SET wikidata_seen_at = now() - interval '60 days'"))
    async with db.transaction() as s:  # no recent import: nothing revoked
        n = await sources.revoke_stale_wikidata(
            s, SYSTEM, stale_days=30, fresh_window=timedelta(days=8)
        )
        assert n == 0
    items["Q12"] = WikidataItem("Q12", "C", types=("Q5",), occupations=("Q901",))
    async with db.transaction() as s:
        await sources.apply_wikidata_batch(s, SYSTEM, [["Q12", "12", ""]], items)
        n = await sources.revoke_stale_wikidata(
            s, SYSTEM, stale_days=30, fresh_window=timedelta(days=8)
        )
        assert n == 1
    async with db.session() as s:
        assert set(await uc.lookup_badges(s, ANON, "x", ["11", "12"])) == {"12"}
    # Revoked by the system: a later import grants it back
    async with db.transaction() as s:
        stats = await sources.apply_wikidata_batch(s, SYSTEM, [["Q11", "11", ""]], items)
    assert stats["granted"] == 1


# --- Service jobs ----------------------------------------------------------------------


class FakeWikidata:
    def __init__(self, accounts: list[WikidataAccount], items: dict[str, WikidataItem]) -> None:
        self.accounts = accounts
        self.data = items

    async def x_accounts(self, scope: str) -> Sequence[WikidataAccount]:
        return self.accounts

    async def items(self, qids: Sequence[str]) -> Mapping[str, WikidataItem]:
        return {q: self.data[q] for q in qids if q in self.data}

    async def aclose(self) -> None:
        return None


class FakeX:
    platform = "x"
    cost_per_account_read_usd_micros = 10_000

    def __init__(self, answers: dict[str, str | None]) -> None:
        self.answers = answers
        self.calls: list[list[str]] = []

    async def get_accounts(self, external_ids: Sequence[str]) -> Sequence[PublicAccount]:
        self.calls.append(list(external_ids))
        return [
            PublicAccount("x", i, display_name=f"Name {i}", verified_type=self.answers[i])
            for i in external_ids
            if i in self.answers
        ]


def ctx(db: Database, settings: Settings, kind: str, payload: dict[str, Any]) -> JobContext:
    job = JobRecord(
        id=1,
        queue="badges",
        kind=kind,
        payload=payload,
        priority=0,
        attempts=1,
        max_attempts=5,
        dedupe_key=None,
        created_at=datetime.now(UTC),
    )
    return JobContext(job=job, db=db, settings=settings, log=structlog.get_logger("test"))


@pytest.fixture
def overrides() -> Iterator[None]:
    from tower.services.badges import clients

    yield None
    clients.set_overrides()


async def test_wikidata_jobs(db: Database, overrides: None) -> None:
    from tower.services import badges as svc
    from tower.services.badges import clients

    fake = FakeWikidata(
        [WikidataAccount("Q1", "1", "a"), WikidataAccount("Q2", "2", None)],
        {
            "Q1": WikidataItem("Q1", "A", types=("Q11032",)),
            "Q2": WikidataItem("Q2", "B", types=("Q5",), occupations=("Q1930187",)),
        },
    )
    clients.set_overrides(wikidata=fake)
    settings = make_settings(badges_wikidata_scopes="Q142")
    async with db.transaction() as s:
        assert await svc.schedule(s, settings) == 1
        assert await svc.schedule(s, settings) == 0  # started recently: not due
    await svc.wikidata_scope(ctx(db, settings, "badges.wikidata_scope", {"scope": "Q142"}))
    async with db.session() as s:
        payload = await s.scalar(
            text("SELECT payload FROM jobs WHERE kind = 'badges.wikidata_batch'")
        )
        run = await sources.import_runs(s, STAFF)
    assert run[0]["accounts"] == 2 and run[0]["batches"] == 1
    await svc.wikidata_batch(ctx(db, settings, "badges.wikidata_batch", payload))
    async with db.session() as s:
        found = await uc.lookup_badges(s, ANON, "x", ["1", "2"])
    assert {k: v["category"] for k, v in found.items()} == {"1": "media", "2": "media"}


async def test_x_verify_job(db: Database, overrides: None) -> None:
    from tower.services import badges as svc
    from tower.services.badges import clients

    fake = FakeX({"21": "government", "22": "blue"})
    clients.set_overrides(x=fake)
    settings = make_settings(badges_x_daily_checks=10)
    async with db.transaction() as s:
        queued = await sources.hint_x_accounts(s, ANON, ["21", "22", "23"], recheck_days=30)
        assert queued == 3
        assert await sources.hint_x_accounts(s, ANON, ["21"], recheck_days=30) == 1
    await svc.x_verify(ctx(db, settings, "badges.x_verify", {}))
    assert sorted(fake.calls[0]) == ["21", "22", "23"]
    async with db.session() as s:
        found = await uc.lookup_badges(s, ANON, "x", ["21", "22", "23"])
        b, _ = await uc.get_badge(s, STAFF, ref("21"))
    assert list(found) == ["21"] and found["21"]["category"] == "institutional"
    assert (b.source, b.label) == ("x_government", "Name 21")
    # Checked recently: a new hint does not queue a second check
    async with db.transaction() as s:
        assert await sources.hint_x_accounts(s, ANON, ["22"], recheck_days=30) == 0
    # Recheck after the delay: X no longer says government → revoked by the system
    async with db.transaction() as s:
        await s.execute(text("UPDATE badge_x_checks SET checked_at = now() - interval '40 days'"))
    fake.answers["21"] = "blue"
    await svc.x_verify(ctx(db, settings, "badges.x_verify", {}))
    async with db.session() as s:
        assert await uc.lookup_badges(s, ANON, "x", ["21"]) == {}
        b, _ = await uc.get_badge(s, STAFF, ref("21"))
    assert b.status == "revoked" and b.end_reason == "x_verification_lost"


async def test_x_verify_daily_cap(db: Database, overrides: None) -> None:
    from tower.services import badges as svc
    from tower.services.badges import clients

    fake = FakeX({})
    clients.set_overrides(x=fake)
    async with db.transaction() as s:
        await sources.hint_x_accounts(s, ANON, ["31", "32", "33"], recheck_days=30)
    await svc.x_verify(ctx(db, make_settings(badges_x_daily_checks=2), "badges.x_verify", {}))
    await svc.x_verify(ctx(db, make_settings(badges_x_daily_checks=2), "badges.x_verify", {}))
    assert [len(c) for c in fake.calls] == [2]


async def test_expire_periodic(db: Database) -> None:
    from tower.services import badges as svc

    await org_with_member(db)
    async with db.transaction() as s:
        await s.execute(text("UPDATE badge_affiliations SET expires_at = now() - interval '1 s'"))
        assert await svc.expire(s, make_settings()) == 1
    async with db.session() as s:
        n = await s.scalar(select(BadgeAffiliation.id).where(BadgeAffiliation.status == "expired"))
    assert n is not None


# --- Gate ---------------------------------------------------------------------------------


def h(token: str, staff: int | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if staff is not None:
        headers["X-Acting-Discord-Id"] = str(staff)
    return headers


@pytest.fixture
async def api(db: Database) -> AsyncIterator[httpx.AsyncClient]:
    from tower.gate.app import create_app

    app = create_app(make_settings(admin_discord_ids=[1]))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gate") as c:
            yield c


async def test_gate_lookup_is_public_and_cacheable(
    api: httpx.AsyncClient, make_token: TokenFactory
) -> None:
    token = await make_token("intercom")
    r = await api.put(
        "/v1/badges/accounts/x/42", json={"category": "media", "label": "AFP"}, headers=h(token, 1)
    )
    assert r.status_code == 200, r.text
    assert r.json()["granted_by"] == "staff:1@intercom"
    r = await api.get("/v1/badges/lookup", params={"ids": "42,43"})
    assert r.status_code == 200
    assert r.json() == {
        "platform": "x",
        "accounts": {
            "42": {
                "category": "media",
                "color": "#D946EF",
                "badge": {"category": "media", "color": "#D946EF", "label": "AFP"},
                "affiliation": None,
            }
        },
    }
    assert r.headers["cache-control"] == "public, max-age=300"
    etag = r.headers["etag"]
    r2 = await api.get(
        "/v1/badges/lookup", params={"ids": "42,43"}, headers={"If-None-Match": etag}
    )
    assert r2.status_code == 304
    r = await api.get("/v1/badges/lookup", params={"ids": "@afp"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "TWR-ING-006"
    r = await api.get("/v1/badges/lookup", params={"ids": ",".join(str(i) for i in range(101))})
    assert r.status_code == 422 and r.json()["error"]["code"] == "TWR-BDG-003"
    r = await api.get("/v1/badges/categories")
    assert [c["category"] for c in r.json()] == [
        "institutional",
        "media",
        "expert",
        "organization",
        "personality",
    ]
    r = await api.get("/v1/badges/accounts/x/42")
    assert r.status_code == 200 and r.json()["badge"]["category"] == "media"
    assert "note" not in r.json()["badge"]
    # Team endpoints need a token
    r = await api.put("/v1/badges/accounts/x/44", json={"category": "media"})
    assert r.status_code == 403


async def test_gate_org_key_manages_affiliates(
    api: httpx.AsyncClient, make_token: TokenFactory, db: Database
) -> None:
    token = await make_token("intercom")
    staff = h(token, 1)
    await api.put("/v1/badges/accounts/x/100", json={"category": "organization"}, headers=staff)
    await api.put("/v1/badges/accounts/x/101", json={"category": "media"}, headers=staff)
    r = await api.post("/v1/badges/accounts/x/100/org-keys", json={"label": "RH"}, headers=staff)
    assert r.status_code == 201
    org_token = r.json()["token"]
    key_id = r.json()["public_id"]
    assert org_token.startswith("t12o_")
    org = {"X-Organization-Token": org_token}
    r = await api.get("/v1/org/me", headers=org)
    assert r.status_code == 200 and r.json()["account"]["external_id"] == "100"
    r = await api.post(
        "/v1/org/affiliations", json={"member": {"external_id": "200"}, "title": "Dev"}, headers=org
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["status"] == "active" and created["validated_by"].startswith("organization:")
    assert created["source"] == "organization"
    # The organisation cannot act on another organisation's affiliation
    r = await api.post(
        "/v1/badges/affiliations",
        json={"member": {"external_id": "201"}, "org": {"external_id": "101"}},
        headers=staff,
    )
    other = r.json()["public_id"]
    r = await api.post(f"/v1/badges/affiliations/{other}/revoke", json={}, headers=org)
    assert r.status_code == 403 and r.json()["error"]["code"] == "TWR-BDG-018"
    r = await api.post(f"/v1/badges/affiliations/{created['public_id']}/reconfirm", headers=org)
    assert r.status_code == 200
    r = await api.get("/v1/org/affiliations", headers=org)
    assert [a["member"]["external_id"] for a in r.json()] == ["200"]
    # Organisation tokens cannot be mixed with another credential, nor reach team routes
    r = await api.get("/v1/org/me", headers={**org, "Authorization": f"Bearer {token}"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "TWR-BDG-017"
    r = await api.get("/v1/badges/requests", headers=org)
    assert r.status_code == 403
    # Revoked key → 401
    await api.post(f"/v1/badges/org-keys/{key_id}/revoke", headers=staff)
    r = await api.get("/v1/org/me", headers=org)
    assert r.status_code == 401 and r.json()["error"]["code"] == "TWR-BDG-016"
    async with db.session() as s:
        n = await s.scalar(select(BadgeEvent.id).where(BadgeEvent.action == "org_key_revoked"))
    assert n is not None


async def test_gate_submissions_and_rate_limit(
    api: httpx.AsyncClient, make_token: TokenFactory
) -> None:
    token = await make_token("intercom")
    staff = h(token, 1)
    r = await api.post(
        "/v1/badges/requests",
        json={"kind": "badge", "account": {"external_id": "700"}, "category": "expert"},
    )
    assert r.status_code == 201, r.text
    request_id = r.json()["public_id"]
    r = await api.get("/v1/badges/requests", headers=staff)
    assert [x["public_id"] for x in r.json()] == [request_id]
    r = await api.post(
        f"/v1/badges/requests/{request_id}/approve", json={"label": "Pr Z"}, headers=staff
    )
    assert r.status_code == 200 and r.json()["status"] == "approved"
    r = await api.post(
        "/v1/badges/reports",
        json={"account": {"external_id": "700"}, "reason": "not_eligible", "comment": "?"},
    )
    assert r.status_code == 201
    r = await api.get("/v1/badges/accounts/x/700/history", headers=staff)
    assert [e["action"] for e in r.json()] == ["granted"]
    r = await api.post("/v1/badges/x-government-hints", json={"ids": ["800"]})
    assert r.status_code == 202 and r.json() == {"queued": 1}
    codes = set()
    for _ in range(12):
        r = await api.post("/v1/badges/x-government-hints", json={"ids": ["801"]})
        codes.add(r.status_code)
    assert 429 in codes


async def test_gate_import_request(api: httpx.AsyncClient, make_token: TokenFactory) -> None:
    token = await make_token("intercom")
    r = await api.post(
        "/v1/badges/imports/wikidata", json={"scopes": ["Q142", "intl"]}, headers=h(token, 1)
    )
    assert r.status_code == 202 and r.json() == {"scopes": ["Q142", "intl"]}
    r = await api.post(
        "/v1/badges/imports/wikidata", json={"scopes": ["DROP"]}, headers=h(token, 1)
    )
    assert r.status_code == 422
    r = await api.get("/v1/badges/imports", headers=h(token, 1))
    assert {x["scope"] for x in r.json()} == {"Q142", "intl"}
