"""Bulk badge seed (``tower badges seed``): CSV validation, name check, grant rules."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from tower.core.badge_seed import (
    SeedRow,
    lookup_wikidata,
    name_matches,
    parse_seed,
    read_seed,
    run_seed,
    seed_one,
)
from tower.core.db.database import Database
from tower.core.domain import error_codes as ec
from tower.core.domain.badges import AccountRef, BadgeCategory
from tower.core.domain.platforms import Platform
from tower.core.domain.security import Actor
from tower.core.models.badges import Badge
from tower.core.models.ingestion import PlatformAccount
from tower.core.ports.badges import WikidataAccount
from tower.core.ports.knowledge import SourceUnavailable
from tower.core.ports.platforms import PublicAccount
from tower.core.usecases import badges as uc

from .conftest import make_settings

HEADER = "handle,category,name,expertise,review,note\n"


def test_shipped_seed_is_valid() -> None:
    parsed = parse_seed(read_seed(None))
    assert parsed.errors == []
    assert len(parsed.rows) > 600
    cats = {r.category for r in parsed.rows}
    assert cats == set(BadgeCategory)
    assert all(r.expertise for r in parsed.rows if r.category is BadgeCategory.EXPERT)


def test_parse_reports_invalid_rows() -> None:
    text = HEADER + (
        "# comment\n"
        "EmmanuelMacron,institutional,Emmanuel Macron,,,\n"
        "emmanuelmacron,verified,Emmanuel Macron,,,\n"
        "FukuyamaInstitute,expert,Francis Fukuyama,Science politique,,\n"
        "someone,celebrity,Someone,,,\n"
        "lemondefr,media,Le Monde,Presse,,\n"
        "noname,verified,,,,\n"
        "ylecun,expert,Yann LeCun,IA,1,note\n"
    )
    parsed = parse_seed(text)
    assert [r.handle for r in parsed.rows] == ["EmmanuelMacron", "ylecun"]
    assert parsed.rows[1].review is True and parsed.rows[1].expertise == "IA"
    assert len(parsed.errors) == 5
    assert "duplicate" in parsed.errors[0]


def test_parse_rejects_bad_header() -> None:
    assert parse_seed("handle,category\nx,verified\n").errors


@pytest.mark.parametrize(
    ("names", "display", "handle", "expected"),
    [
        (("Emmanuel Macron",), "Emmanuel Macron", "EmmanuelMacron", True),
        (("Élysée|x",), "Elysee", "Elysee", True),
        (("Recep Tayyip Erdoğan",), "Recep Tayyip Erdogan", "RTErdogan", True),
        (("Christopher White",), "Chris White", "Chris_WhiteUK", True),
        (("Héctor Muñoz",), "Fan page", "hmunozalcalde", False),
        (("Mofa Japan",), "外務省", "MofaJapan_jp", True),
        (("Mofa Japan",), "外務省", "Other_jp", False),
        (("Adama Barrow",), "Someone Else", "random_123", False),
        (("Le Monde",), None, None, False),
    ],
)
def test_name_matches(
    names: tuple[str, ...], display: str | None, handle: str | None, expected: bool
) -> None:
    assert name_matches(names, display, handle) is expected


class FakeX:
    cost_per_account_read_usd_micros = 10_000

    def __init__(self, accounts: dict[str, PublicAccount]) -> None:
        self.accounts = {k.lower(): v for k, v in accounts.items()}
        self.calls: list[str] = []

    async def get_account(self, ref: str) -> PublicAccount | None:
        self.calls.append(ref)
        return self.accounts.get(ref.lower())


def _row(handle: str, name: str, category: str = "institutional", review: bool = False) -> SeedRow:
    return SeedRow(2, handle, BadgeCategory(category), (name,), None, review, "test")


def _acct(xid: str, handle: str, name: str, vtype: str | None = None) -> PublicAccount:
    return PublicAccount("x", xid, handle=handle, display_name=name, verified_type=vtype)


async def _badge(db: Database, xid: str) -> Badge | None:
    async with db.session() as s:
        return await s.scalar(
            select(Badge).join(PlatformAccount).where(PlatformAccount.external_id == xid)
        )


async def test_seed_one_grants_and_skips(db: Database) -> None:
    fake = FakeX(
        {
            "EmmanuelMacron": _acct("1976143068", "EmmanuelMacron", "Emmanuel Macron", "blue"),
            "Impostor": _acct("42", "Impostor", "Totally Someone", "blue"),
            "Obscure": _acct("43", "Obscure", "Obscure Mayor", "business"),
            "NoCheck": _acct("45", "NoCheck", "No Check", "none"),
            "GovMayor": _acct("44", "GovMayor", "Gov Mayor", "government"),
        }
    )
    out = await seed_one(
        db, fake, _row("EmmanuelMacron", "Emmanuel Macron"), ttl_s=600, include_review=False
    )
    assert out.status == "granted"
    badge = await _badge(db, "1976143068")
    assert badge is not None and badge.status == "active"
    assert badge.category == "institutional" and badge.source == "manual"
    assert badge.granted_by == "system:badges-seed"

    # Rerun: known by stored handle, no X call.
    fake.calls.clear()
    out = await seed_one(
        db,
        fake,
        _row("emmanuelmacron", "Emmanuel Macron", "verified"),
        ttl_s=600,
        include_review=False,
    )
    assert out.status == "exists" and fake.calls == []

    out = await seed_one(
        db, fake, _row("Impostor", "Adama Barrow"), ttl_s=600, include_review=False
    )
    assert out.status == "name_mismatch" and await _badge(db, "42") is None
    out = await seed_one(db, fake, _row("Nobody", "Nobody"), ttl_s=600, include_review=False)
    assert out.status == "not_found"
    out = await seed_one(
        db, fake, _row("Obscure", "Obscure Mayor", review=True), ttl_s=600, include_review=False
    )
    assert out.status == "review" and await _badge(db, "43") is None
    out = await seed_one(
        db, fake, _row("GovMayor", "Gov Mayor", review=True), ttl_s=600, include_review=False
    )
    assert out.status == "granted"
    out = await seed_one(
        db, fake, _row("Obscure", "Obscure Mayor", review=True), ttl_s=600, include_review=True
    )
    assert out.status == "granted"
    out = await seed_one(db, fake, _row("NoCheck", "No Check"), ttl_s=600, include_review=False)
    assert out.status == "no_check" and await _badge(db, "45") is None


async def test_seed_one_uses_wikidata_without_x(db: Database) -> None:
    fake = FakeX({"Amb": _acct("61", "Amb", "Amb", "government")})
    wd: dict[str, str | None] = {"wdperson": "60", "amb": None}
    out = await seed_one(
        db,
        fake,
        _row("WdPerson", "Anything", review=True),
        ttl_s=600,
        include_review=False,
        wikidata=wd,
    )
    assert out.status == "granted" and "(wikidata)" in out.detail and fake.calls == []
    badge = await _badge(db, "60")
    assert badge is not None and badge.category == "institutional"
    assert "wikidata" in (badge.note or "")
    # Ambiguous Wikidata ids: resolved with X instead.
    out = await seed_one(db, fake, _row("Amb", "Amb"), ttl_s=600, include_review=False, wikidata=wd)
    assert out.status == "granted" and fake.calls == ["Amb"]
    # Not on Wikidata and no X client: skipped, never guessed.
    out = await seed_one(
        db, None, _row("Other", "Other"), ttl_s=600, include_review=False, wikidata=wd
    )
    assert out.status == "needs_x"


class FakeWikidata:
    def __init__(self, accounts: list[WikidataAccount] | None = None, fail: bool = False) -> None:
        self.accounts = accounts or []
        self.fail = fail
        self.asked: list[list[str]] = []

    async def accounts_by_handles(self, handles: list[str]) -> list[WikidataAccount]:
        self.asked.append(list(handles))
        if self.fail:
            raise SourceUnavailable("down", code=ec.ATL_SOURCE_UNAVAILABLE, source="wikidata")
        return [a for a in self.accounts if a.handle in handles]

    async def aclose(self) -> None:
        return None


async def test_lookup_wikidata_case_and_ambiguity() -> None:
    src = FakeWikidata(
        [
            WikidataAccount("Q1", "100", "emmanuelmacron"),
            WikidataAccount("Q2", "200", "Dup"),
            WikidataAccount("Q3", "201", "Dup"),
            WikidataAccount("Q4", "not-an-id", "Bad"),
        ]
    )
    ids = await lookup_wikidata(src, ["EmmanuelMacron", "Dup", "Bad", "Missing"])
    assert ids == {"emmanuelmacron": "100", "dup": None}
    assert "emmanuelmacron" in src.asked[0] and "EmmanuelMacron" in src.asked[0]


async def test_seed_one_respects_team_decisions(db: Database) -> None:
    ref = AccountRef(Platform.X, "50")
    async with db.transaction() as s:
        await uc.grant_badge(s, _staff(), ref, category="media", handle="OldHandle")
    async with db.transaction() as s:
        await uc.revoke_badge(s, _staff(), ref, reason="team decision")
    fake = FakeX({"NewHandle": _acct("50", "NewHandle", "Some Media", "business")})
    out = await seed_one(db, fake, _row("NewHandle", "Some Media"), ttl_s=600, include_review=False)
    assert out.status == "exists" and "revoked" in out.detail
    badge = await _badge(db, "50")
    assert badge is not None and badge.status == "revoked"


def _staff() -> Actor:
    from tower.core.domain.security import ActorKind, Permission

    return Actor(ActorKind.STAFF, "1", frozenset(Permission), via="intercom")


async def test_run_seed_dry_run_calls_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "seed.csv"
    path.write_text(HEADER + "EmmanuelMacron,institutional,Emmanuel Macron,,,\nbad!,x,y,,,\n")
    code = await run_seed(
        make_settings(),
        path=str(path),
        apply=False,
        category=None,
        limit=None,
        include_review=False,
        wikidata_source=FakeWikidata([WikidataAccount("Q1", "100", "EmmanuelMacron")]),
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "plan  @EmmanuelMacron" in out and "wikidata" in out and "invalid=1" in out
    assert "X reads at most: 0" in out and "Dry run" in out


async def test_run_seed_stops_when_wikidata_is_down(capsys: pytest.CaptureFixture[str]) -> None:
    code = await run_seed(
        make_settings(),
        path=None,
        apply=True,
        category="media",
        limit=2,
        include_review=False,
        wikidata_source=FakeWikidata(fail=True),
    )
    assert code == 1 and "Wikidata unavailable" in capsys.readouterr().out


async def test_run_seed_apply_without_x_token(
    db: Database, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "seed.csv"
    path.write_text(HEADER + "lemondefr,media,Le Monde,,,\nliberation,media,Libération,,,\n")
    code = await run_seed(
        make_settings(x_bearer_token=None),
        path=str(path),
        apply=True,
        category=None,
        limit=None,
        include_review=False,
        wikidata_source=FakeWikidata([WikidataAccount("Q12461", "24744541", "lemondefr")]),
    )
    out = capsys.readouterr().out
    assert code == 0 and "TOWER_X_BEARER_TOKEN" in out
    assert "granted=1" in out and "needs_x=1" in out
    badge = await _badge(db, "24744541")
    assert badge is not None and badge.category == "media"
