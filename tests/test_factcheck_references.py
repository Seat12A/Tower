"""Reference facts and value bands (lot 4): creation, validation, bands, expiry at review."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from tests.test_drafts_usecases import Built, build_draft
from tower.core.db.database import Database
from tower.core.domain import error_codes as ec
from tower.core.domain.errors import InvalidInput, PermissionDenied
from tower.core.domain.factcheck.values import (
    Band,
    BandSide,
    ContextNeed,
    FactOrigin,
    Veracity,
)
from tower.core.domain.security import Actor, Permission, Role
from tower.core.models.events import Event
from tower.core.models.factcheck import Claim, ClaimGroup, ContextNote, Verdict
from tower.core.models.factcheck_v1 import ReferenceFact, VerdictBand
from tower.core.usecases import drafts as drafts_uc
from tower.core.usecases import references as uc

SERVICE = Actor.service("intercom", frozenset(Permission))
REVIEWER = Actor.staff(1001, frozenset({Role.REVIEWER}), SERVICE)
EDITOR = Actor.staff(1002, frozenset({Role.EDITOR}), SERVICE)

Q1 = (date(2026, 1, 1), date(2026, 3, 31))
Q2 = (date(2026, 4, 1), date(2026, 6, 30))


def fact_args(**over: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "indicator": "fr.unemployment_rate",
        "label": "Taux de chômage (BIT), France",
        "value": Decimal("7.3"),
        "unit": "%",
        "period_start": Q1[0],
        "period_end": Q1[1],
        "territory": "fr",
        "source_url": "https://www.insee.fr/serie/001688527",
        "source_name": "INSEE",
        "published_at": date(2026, 5, 15),
        "aliases": ["Taux de chômage", "chômage"],
        "origin": FactOrigin.MANUAL,
    }
    args.update(over)
    return args


async def new_fact(db: Database, *, validate: bool = True, **over: Any) -> ReferenceFact:
    async with db.transaction() as s:
        fact = await uc.create_reference_fact(s, EDITOR, **fact_args(**over))
        if validate:
            await uc.validate_reference_fact(s, EDITOR, fact_id=fact.public_id)
        return fact


async def topic_events(db: Database, topic: str) -> list[Event]:
    async with db.session() as s:
        return list(
            (await s.scalars(select(Event).where(Event.topic == topic).order_by(Event.id))).all()
        )


async def prepare_group(
    db: Database,
    built: Built,
    *,
    low: str | None = "8",
    high: str | None = "9",
    fact: ReferenceFact | None = None,
) -> None:
    """Give the claim a percent interval and a territory (and link a fact if given)."""
    async with db.transaction() as s:
        claim = await s.get(Claim, built.claim_id)
        group = await s.get(ClaimGroup, built.group_id)
        assert claim is not None and group is not None
        claim.subject = "Taux de chômage"
        claim.predicate = "unemployment_rate"
        claim.unit = "percent"
        claim.place = "FR"
        claim.value_low = Decimal(low) if low else None
        claim.value_high = Decimal(high) if high else None
        if fact is not None:
            group.reference_fact_id = fact.id


async def active_draft(db: Database, ext: str, **kw: Any) -> Built:
    built = await build_draft(db, ext=ext)
    await prepare_group(db, built, **kw)
    async with db.transaction() as s:
        await drafts_uc.review_draft(s, EDITOR, built.verdict, decision="accept")
    return built


# --- creation -----------------------------------------------------------------------


async def test_create_fact_normalizes_and_is_pending(db: Database) -> None:
    fact = await new_fact(db, validate=False)
    assert fact.status == "pending" and fact.territory == "FR" and fact.unit == "percent"
    assert fact.aliases == ["taux de chomage", "chomage"]
    assert fact.created_by == EDITOR.label and fact.validated_at is None


@pytest.mark.parametrize(
    "over",
    [
        {"period_start": date(2026, 4, 1), "period_end": date(2026, 3, 1)},
        {"territory": "France"},
        {"source_url": "ftp://example.org/x"},
        {"source_url": "not a url"},
        {"unit": " "},
        {"unit": "percent", "unit_scale": "gazillion"},
        {"indicator": "Bad Key"},
        {"label": " "},
        {"value": Decimal("NaN")},
    ],
)
async def test_create_fact_rejects_invalid_input(db: Database, over: dict[str, Any]) -> None:
    async with db.transaction() as s:
        with pytest.raises(InvalidInput) as exc:
            await uc.create_reference_fact(s, EDITOR, **fact_args(**over))
    assert exc.value.code == ec.FCK_FACT_INVALID.code


async def test_create_fact_requires_permission(db: Database) -> None:
    async with db.transaction() as s:
        with pytest.raises(PermissionDenied):
            await uc.create_reference_fact(s, Actor.anonymous(), **fact_args())


async def test_create_fact_is_idempotent_on_external_key(db: Database) -> None:
    first = await new_fact(db, validate=False, external_key="insee:1:2026-Q1")
    async with db.transaction() as s:
        again = await uc.create_reference_fact(
            s, EDITOR, **fact_args(external_key="insee:1:2026-Q1", value=Decimal("9"))
        )
        assert again.id == first.id and again.value == Decimal("7.3")
        count = len((await s.scalars(select(ReferenceFact))).all())
    assert count == 1


async def test_previous_fact_defaults_to_latest_validated_of_the_series(db: Database) -> None:
    first = await new_fact(db)
    await new_fact(
        db, validate=False, period_start=date(2025, 10, 1), period_end=date(2025, 12, 31)
    )
    second = await new_fact(db, period_start=Q2[0], period_end=Q2[1], value=Decimal("7.5"))
    other = await new_fact(db, indicator="fr.other", period_start=Q2[0], period_end=Q2[1])
    assert second.previous_fact_id == first.id
    assert other.previous_fact_id is None
    # Pending older fact was not chosen (not validated): first has no predecessor.
    assert first.previous_fact_id is None


# --- validation ---------------------------------------------------------------------


async def test_validate_fact_publishes_and_sets_bands(db: Database) -> None:
    built = await build_draft(db, ext="1850000000000000101")
    fact = await new_fact(db, validate=False)
    await prepare_group(db, built, low="8", high="9", fact=fact)
    async with db.transaction() as s:
        await uc.validate_reference_fact(s, EDITOR, fact_id=fact.public_id)
    async with db.session() as s:
        group = await s.get(ClaimGroup, built.group_id)
        row = await s.get(ReferenceFact, fact.id)
        assert row is not None and row.status == "validated"
        assert row.validated_by == EDITOR.label and row.validated_at is not None
        assert group is not None
        # reference 7.3, tau = max(0.2, 0.05*7.3) = 0.365; claim [8, 9[ -> 0.7 = 2 tau
        assert (group.band, group.band_side) == ("within_3_tau", "above")
    (event,) = await topic_events(db, "reference_fact.validated")
    assert event.payload["fact_id"] == str(fact.public_id)
    assert event.payload["period_end"] == "2026-03-31" and event.payload["territory"] == "FR"


async def test_validate_fact_is_idempotent_and_needs_permission(db: Database) -> None:
    fact = await new_fact(db)
    async with db.transaction() as s:
        await uc.validate_reference_fact(s, EDITOR, fact_id=fact.public_id)
    assert len(await topic_events(db, "reference_fact.validated")) == 1
    other = await new_fact(db, validate=False, indicator="fr.x")
    async with db.transaction() as s:
        with pytest.raises(PermissionDenied):
            await uc.validate_reference_fact(s, REVIEWER, fact_id=other.public_id)


async def test_reject_fact(db: Database) -> None:
    fact = await new_fact(db, validate=False)
    async with db.transaction() as s:
        with pytest.raises(InvalidInput):
            await uc.reject_reference_fact(s, EDITOR, fact_id=fact.public_id, reason=" ")
        rejected = await uc.reject_reference_fact(
            s, EDITOR, fact_id=fact.public_id, reason="wrong series"
        )
        assert rejected.status == "rejected"
        with pytest.raises(InvalidInput):
            await uc.validate_reference_fact(s, EDITOR, fact_id=fact.public_id)


# --- from a verdict -----------------------------------------------------------------


async def test_create_fact_from_verdict(db: Database) -> None:
    built = await active_draft(db, "1850000000000000102")
    async with db.transaction() as s:
        fact = await uc.create_fact_from_verdict(
            s,
            EDITOR,
            verdict_id=built.verdict,
            value=Decimal("7.3"),
            period_start=Q1[0],
            period_end=Q1[1],
            source_url="https://www.insee.fr/x",
            source_name="INSEE",
            published_at=date(2026, 5, 15),
        )
        again = await uc.create_fact_from_verdict(
            s,
            EDITOR,
            verdict_id=built.verdict,
            value=Decimal("7.3"),
            period_start=Q1[0],
            period_end=Q1[1],
            source_url="https://www.insee.fr/x",
            source_name="INSEE",
            published_at=date(2026, 5, 15),
        )
        assert again.id == fact.id
        group = await s.get(ClaimGroup, built.group_id)
        assert group is not None and group.reference_fact_id == fact.id and group.band is None
    assert fact.indicator == "fr.unemployment_rate" and fact.unit == "percent"
    assert fact.origin == "investigation" and fact.status == "pending"
    assert fact.aliases == ["taux de chomage"] and fact.origin_verdict_id == built.verdict_id
    async with db.transaction() as s:
        await uc.validate_reference_fact(s, EDITOR, fact_id=fact.public_id)
        group = await s.get(ClaimGroup, built.group_id)
        assert group is not None and group.band == "within_3_tau"


async def test_create_fact_from_verdict_refusals(db: Database) -> None:
    draft = await build_draft(db, ext="1850000000000000103")
    await prepare_group(db, draft)
    args: dict[str, Any] = {
        "value": Decimal("7.3"),
        "period_start": Q1[0],
        "period_end": Q1[1],
        "source_url": "https://www.insee.fr/x",
        "source_name": "INSEE",
        "published_at": date(2026, 5, 15),
    }
    async with db.transaction() as s:
        with pytest.raises(InvalidInput) as exc:  # still a draft
            await uc.create_fact_from_verdict(s, EDITOR, verdict_id=draft.verdict, **args)
    assert exc.value.code == ec.FCK_FACT_INVALID.code
    active = await active_draft(db, "1850000000000000104")
    async with db.transaction() as s:
        claim = await s.get(Claim, active.claim_id)
        assert claim is not None
        claim.claim_type = "quote"
    async with db.transaction() as s:
        with pytest.raises(InvalidInput):
            await uc.create_fact_from_verdict(s, EDITOR, verdict_id=active.verdict, **args)


# --- bands --------------------------------------------------------------------------


async def linked_source(db: Database, fact: ReferenceFact, ext: str) -> Built:
    built = await active_draft(db, ext)
    async with db.transaction() as s:
        group = await s.get(ClaimGroup, built.group_id)
        assert group is not None
        group.reference_fact_id = fact.id
    return built


def band_args(fact: ReferenceFact, **over: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "fact_id": fact.public_id,
        "band": Band.WITHIN_1_TAU,
        "side": BandSide.ABOVE,
        "veracity": Veracity.MOSTLY_TRUE,
        "context_need": ContextNeed.USEFUL,
        "source_verdict_id": None,
    }
    args.update(over)
    return args


async def test_validate_band_rules(db: Database) -> None:
    pending = await new_fact(db, validate=False, indicator="fr.pending")
    fact = await new_fact(db)
    source = await linked_source(db, fact, "1850000000000000105")
    foreign = await active_draft(db, "1850000000000000106")
    async with db.transaction() as s:
        with pytest.raises(InvalidInput) as exc:
            await uc.validate_band(s, EDITOR, **band_args(pending))
        assert exc.value.code == ec.FCK_FACT_NOT_VALIDATED.code
        for band, side in (
            (Band.CONTAINS, BandSide.ABOVE),
            (Band.BEYOND, BandSide.NONE),
        ):
            with pytest.raises(InvalidInput) as exc:
                await uc.validate_band(s, EDITOR, **band_args(fact, band=band, side=side))
            assert exc.value.code == ec.FCK_BAND_INVALID.code
        with pytest.raises(InvalidInput) as exc:  # verdict of a group not linked to the fact
            await uc.validate_band(s, EDITOR, **band_args(fact, source_verdict_id=foreign.verdict))
        assert exc.value.code == ec.FCK_BAND_INVALID.code
        with pytest.raises(PermissionDenied):
            await uc.validate_band(s, REVIEWER, **band_args(fact))
        row = await uc.validate_band(
            s,
            EDITOR,
            **band_args(fact, source_verdict_id=source.verdict, context_types=["outdated"]),
        )
    assert row.status == "active" and row.source_verdict_id == source.verdict_id
    (event,) = await topic_events(db, "verdict_band.validated")
    assert event.payload["band_id"] == str(row.public_id) and event.payload["side"] == "above"


async def test_validate_band_revokes_the_previous_active_one(db: Database) -> None:
    fact = await new_fact(db)
    async with db.transaction() as s:
        first = await uc.validate_band(s, EDITOR, **band_args(fact))
        other = await uc.validate_band(
            s, EDITOR, **band_args(fact, band=Band.BEYOND, veracity=Veracity.FALSE)
        )
    async with db.transaction() as s:
        second = await uc.validate_band(s, EDITOR, **band_args(fact, veracity=Veracity.TRUE))
    async with db.session() as s:
        rows = {r.id: r for r in (await s.scalars(select(VerdictBand))).all()}
    assert rows[first.id].status == "revoked" and rows[first.id].revoked_by == EDITOR.label
    assert rows[second.id].status == "active" and rows[other.id].status == "active"


async def test_validate_band_from_verdict_and_revoke(db: Database) -> None:
    fact = await new_fact(db)
    source = await linked_source(db, fact, "1850000000000000107")
    async with db.transaction() as s:
        group = await s.get(ClaimGroup, source.group_id)
        assert group is not None
        group.band, group.band_side = "within_3_tau", "above"
    async with db.transaction() as s:
        row = await uc.validate_band_from_verdict(s, EDITOR, verdict_id=source.verdict)
    assert (row.band, row.side) == ("within_3_tau", "above")
    assert row.veracity == "false" and row.context_need == "necessary"
    assert row.source_verdict_id == source.verdict_id
    async with db.transaction() as s:
        revoked = await uc.revoke_band(s, EDITOR, band_id=row.public_id, reason="superseded study")
        assert revoked.status == "revoked"
        with pytest.raises(InvalidInput):
            await uc.revoke_band(s, EDITOR, band_id=row.public_id, reason="again")


async def test_validate_band_from_verdict_needs_band_and_active_verdict(db: Database) -> None:
    fact = await new_fact(db)
    source = await linked_source(db, fact, "1850000000000000108")
    async with db.transaction() as s:
        with pytest.raises(InvalidInput) as exc:  # group has no band yet
            await uc.validate_band_from_verdict(s, EDITOR, verdict_id=source.verdict)
    assert exc.value.code == ec.FCK_BAND_INVALID.code
    draft = await build_draft(db, ext="1850000000000000109")
    async with db.transaction() as s:
        with pytest.raises(InvalidInput):
            await uc.validate_band_from_verdict(s, EDITOR, verdict_id=draft.verdict)


async def test_review_can_validate_the_band_in_the_same_transaction(db: Database) -> None:
    fact = await new_fact(db)
    built = await build_draft(db, ext="1850000000000000110")
    await prepare_group(db, built, fact=fact)
    async with db.transaction() as s:
        group = await s.get(ClaimGroup, built.group_id)
        assert group is not None
        group.band, group.band_side = "within_3_tau", "above"
    async with db.transaction() as s:
        await drafts_uc.review_draft(
            s, EDITOR, built.verdict, decision="accept", validate_band=True
        )
    async with db.session() as s:
        (row,) = (await s.scalars(select(VerdictBand))).all()
    assert row.source_verdict_id == built.verdict_id and row.status == "active"


# --- expiry at review ---------------------------------------------------------------


async def reviewed_verdict(db: Database, ext: str, **setup: Any) -> Verdict:
    built = await build_draft(db, ext=ext)
    async with db.transaction() as s:
        group = await s.get(ClaimGroup, built.group_id)
        assert group is not None
        if "fact" in setup:
            group.reference_fact_id = setup["fact"].id
        if "provisional" in setup:
            note = await s.get(ContextNote, built.note_id)
            assert note is not None
            note.regime = "provisional"
            note.expires_at = datetime.now(UTC) + timedelta(hours=6)
    async with db.transaction() as s:
        await drafts_uc.review_draft(s, EDITOR, built.verdict, decision="accept")
    async with db.session() as s:
        verdict = await s.get(Verdict, built.verdict_id)
        assert verdict is not None
        return verdict


def near(value: datetime | None, delta: timedelta) -> bool:
    assert value is not None
    return abs(value - (datetime.now(UTC) + delta)) < timedelta(minutes=5)


async def test_expiry_uses_the_statistic_ttl(db: Database) -> None:
    verdict = await reviewed_verdict(db, "1850000000000000111")
    assert near(verdict.expires_at, timedelta(days=120))


async def test_expiry_is_capped_by_next_publication(db: Database) -> None:
    soon = datetime.now(UTC).date() + timedelta(days=10)
    fact = await new_fact(db, next_publication=soon)
    verdict = await reviewed_verdict(db, "1850000000000000112", fact=fact)
    assert verdict.expires_at == datetime(soon.year, soon.month, soon.day, tzinfo=UTC)


async def test_expiry_is_capped_to_six_hours_for_a_provisional_note(db: Database) -> None:
    verdict = await reviewed_verdict(db, "1850000000000000113", provisional=True)
    assert near(verdict.expires_at, timedelta(hours=6))
