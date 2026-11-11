from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from tower.core.db.database import Database
from tower.core.domain import error_codes as ec
from tower.core.domain.errors import InvalidInput, NotFound, PermissionDenied
from tower.core.domain.factcheck.values import OverrideTarget
from tower.core.domain.security import Actor, Permission, Role
from tower.core.factcheck import names
from tower.core.factcheck.levels import override_level
from tower.core.models.events import Event
from tower.core.models.factcheck import ClaimGroup, Investigation
from tower.core.models.factcheck_v1 import ClaimGroupEvent, LevelOverride
from tower.core.models.ingestion import ContentItem, PlatformAccount
from tower.core.usecases import levels as uc

from .test_drafts_usecases import Built, build_draft

SERVICE = Actor.service("intercom", frozenset(Permission))
EDITOR = Actor.staff(1002, frozenset({Role.EDITOR}), SERVICE)
REVIEWER = Actor.staff(1001, frozenset({Role.REVIEWER}), SERVICE)


async def events(db: Database, topic: str) -> list[Event]:
    async with db.session() as s:
        return list((await s.scalars(select(Event).where(Event.topic == topic))).all())


async def public_ids(db: Database, built: Built) -> tuple[str, str]:
    """``(group public id, content public id)``."""
    async with db.session() as s:
        group = await s.get(ClaimGroup, built.group_id)
        content = await s.get(ContentItem, built.content_id)
        assert group is not None and content is not None
        return str(group.public_id), str(content.public_id)


async def group_level(db: Database, built: Built) -> str:
    async with db.session() as s:
        group = await s.get(ClaimGroup, built.group_id)
        assert group is not None
        return group.level


async def create(
    db: Database,
    target_type: OverrideTarget,
    ref: str,
    level: str,
    *,
    hours: float = 24,
    reason: str = "because",
    actor: Actor = EDITOR,
) -> uc.OverrideResult:
    async with db.transaction() as s:
        return await uc.create_override(
            s,
            actor,
            target_type=target_type,
            target_ref=ref,
            level=level,
            hours=hours,
            reason=reason,
        )


async def test_group_override_raises_the_level(db: Database) -> None:
    built = await build_draft(db)
    group, _ = await public_ids(db, built)
    result = await create(db, OverrideTarget.GROUP, group, "N2")  # N3 -> N2 (assembled)
    assert result.applied and result.groups_changed == 1 and result.target == group
    assert await group_level(db, built) == "N2"
    async with db.session() as s:
        journal = (await s.scalars(select(ClaimGroupEvent))).all()
    assert [(e.kind, e.from_level, e.to_level, e.actor) for e in journal] == [
        ("level_changed", "N3", "N2", EDITOR.label)
    ]
    (changed,) = await events(db, names.CLAIM_GROUP_LEVEL_CHANGED)
    assert changed.payload == {
        "group_id": group,
        "from_level": "N3",
        "to_level": "N2",
        "reason": "override",
    }
    (created,) = await events(db, names.LEVEL_OVERRIDE_CREATED)
    assert created.payload["override_id"] == str(result.override.public_id)
    assert created.payload["target_type"] == "group" and created.payload["target_id"] == group
    assert created.payload["level"] == "N2" and created.payload["actor"] == EDITOR.label
    assert created.payload["reason"] == "because" and created.payload["expires_at"]


async def test_same_level_changes_nothing(db: Database) -> None:
    built = await build_draft(db)
    group, _ = await public_ids(db, built)
    result = await create(db, OverrideTarget.GROUP, group, "N3")
    assert result.applied and result.groups_changed == 0
    assert await events(db, names.CLAIM_GROUP_LEVEL_CHANGED) == []


@pytest.mark.parametrize("by", ["public", "external"])
async def test_content_override_by_public_or_external_id(db: Database, by: str) -> None:
    built = await build_draft(db)
    _, content = await public_ids(db, built)
    ref = content if by == "public" else "x:1850000000000000042"
    result = await create(db, OverrideTarget.CONTENT, ref, "N1")
    assert result.applied and result.groups_changed == 1
    assert await group_level(db, built) == "N1"
    assert result.target == (content if by == "public" else "1850000000000000042")


async def test_n4_promotion_opens_an_investigation(db: Database) -> None:
    built = await build_draft(db)
    _, content = await public_ids(db, built)
    result = await create(db, OverrideTarget.CONTENT, content, "N4")
    assert result.applied
    assert await group_level(db, built) == "N4"
    async with db.session() as s:
        investigations = (
            await s.scalars(
                select(Investigation).where(
                    Investigation.claim_group_id == built.group_id, Investigation.level == "N4"
                )
            )
        ).all()
    assert len(investigations) == 1 and investigations[0].stage == "framing"
    assert investigations[0].requested_by == EDITOR.label
    assert len(await events(db, "investigation.requested")) == 1


async def test_n4_group_override_uses_the_latest_content(db: Database) -> None:
    built = await build_draft(db)
    group, _ = await public_ids(db, built)
    await create(db, OverrideTarget.GROUP, group, "N4")
    async with db.session() as s:
        inv = await s.scalar(select(Investigation).where(Investigation.level == "N4"))
    assert inv is not None and inv.content_id == built.content_id


async def test_lowering_is_deferred_while_an_investigation_runs(db: Database) -> None:
    built = await build_draft(db)
    group, _ = await public_ids(db, built)
    async with db.transaction() as s:
        await s.execute(update(Investigation).values(stage="judging"))
    result = await create(db, OverrideTarget.GROUP, group, "N1")
    assert not result.applied and result.groups_changed == 0
    assert await group_level(db, built) == "N3"  # kept (monotone)
    async with db.session() as s:
        assert (await s.scalars(select(LevelOverride))).one().level == "N1"  # but recorded
    assert await events(db, names.CLAIM_GROUP_LEVEL_CHANGED) == []
    # Raising is allowed while it runs.
    raised = await create(db, OverrideTarget.GROUP, group, "N4")
    assert raised.applied and await group_level(db, built) == "N4"


async def test_account_override_only_records(db: Database) -> None:
    async with db.transaction() as s:
        account = PlatformAccount(platform="x", external_id="4242", handle="Someone")
        s.add(account)
        await s.flush()
        account_id = account.id
    by_handle = await create(db, OverrideTarget.ACCOUNT, "x:@someone", "N3")
    by_id = await create(db, OverrideTarget.ACCOUNT, "x:4242", "N2")
    assert not by_handle.applied and not by_id.applied
    assert by_handle.target == "4242"
    assert by_handle.override.target_id == account_id
    async with db.session() as s:
        got = await override_level(
            s, content_id=None, group_ids=[], account_id=account_id, now=datetime.now(UTC)
        )
    assert got == "N2"  # most recent first


@pytest.mark.parametrize(
    ("target_type", "ref"),
    [
        (OverrideTarget.GROUP, str(uuid.uuid4())),
        (OverrideTarget.CONTENT, str(uuid.uuid4())),
        (OverrideTarget.CONTENT, "x:999"),
        (OverrideTarget.ACCOUNT, "x:@nobody"),
        (OverrideTarget.ACCOUNT, "x:12345"),
    ],
)
async def test_unresolvable_target(db: Database, target_type: OverrideTarget, ref: str) -> None:
    with pytest.raises(NotFound) as exc:
        await create(db, target_type, ref, "N2")
    assert exc.value.error_code is ec.FCK_OVERRIDE_NOT_FOUND


@pytest.mark.parametrize(
    ("target_type", "ref", "level", "hours", "reason"),
    [
        (OverrideTarget.GROUP, "not-a-uuid", "N2", 1, "r"),
        (OverrideTarget.CONTENT, "nocolon", "N2", 1, "r"),
        (OverrideTarget.CONTENT, "myspace:1", "N2", 1, "r"),
        (OverrideTarget.ACCOUNT, "x:@", "N2", 1, "r"),
        (OverrideTarget.GROUP, str(uuid.uuid4()), "N9", 1, "r"),
        (OverrideTarget.GROUP, str(uuid.uuid4()), "N2", 0, "r"),
        (OverrideTarget.GROUP, str(uuid.uuid4()), "N2", 721, "r"),
        (OverrideTarget.GROUP, str(uuid.uuid4()), "N2", float("nan"), "r"),
        (OverrideTarget.GROUP, str(uuid.uuid4()), "N2", 1, "  "),
        (OverrideTarget.GROUP, " ", "N2", 1, "r"),
    ],
)
async def test_bad_input(
    db: Database, target_type: OverrideTarget, ref: str, level: str, hours: float, reason: str
) -> None:
    with pytest.raises(InvalidInput) as exc:
        await create(db, target_type, ref, level, hours=hours, reason=reason)
    assert exc.value.error_code is ec.FCK_OVERRIDE_INVALID


async def test_permission(db: Database) -> None:
    built = await build_draft(db)
    group, _ = await public_ids(db, built)
    with pytest.raises(PermissionDenied):
        await create(db, OverrideTarget.GROUP, group, "N2", actor=REVIEWER)
    async with db.transaction() as s:
        with pytest.raises(PermissionDenied):
            await uc.revoke_override(s, REVIEWER, override_id=uuid.uuid4(), reason="x")


async def test_revoke_keeps_the_group_level(db: Database) -> None:
    built = await build_draft(db)
    group, _ = await public_ids(db, built)
    result = await create(db, OverrideTarget.GROUP, group, "N2")
    async with db.transaction() as s:
        row = await uc.revoke_override(
            s, EDITOR, override_id=result.override.public_id, reason="done"
        )
        assert row.revoked_at is not None and row.revoked_by == EDITOR.label
    assert await group_level(db, built) == "N2"
    async with db.transaction() as s:
        assert await uc.list_overrides(s, EDITOR, active=True) == []
        assert len(await uc.list_overrides(s, EDITOR, active=False)) == 1
        with pytest.raises(InvalidInput):
            await uc.revoke_override(
                s, EDITOR, override_id=result.override.public_id, reason="twice"
            )
        with pytest.raises(NotFound) as exc:
            await uc.revoke_override(s, EDITOR, override_id=uuid.uuid4(), reason="x")
        assert exc.value.error_code is ec.FCK_OVERRIDE_NOT_FOUND


async def test_override_level_precedence_and_expiry(db: Database) -> None:
    built = await build_draft(db)
    group, content = await public_ids(db, built)
    now = datetime.now(UTC)
    async with db.session() as s:
        assert await override_level(s, content_id=1, group_ids=[], account_id=None, now=now) is None
        assert await override_level(s, content_id=None, group_ids=[], account_id=None) is None
    async with db.transaction() as s:
        account = PlatformAccount(platform="x", external_id="77", handle="a")
        s.add(account)
        await s.flush()
        account_id = account.id
    await create(db, OverrideTarget.ACCOUNT, "x:77", "N1")
    async with db.session() as s:
        got = await override_level(
            s, content_id=built.content_id, group_ids=[built.group_id], account_id=account_id
        )
    assert got == "N1"
    first_group = await create(db, OverrideTarget.GROUP, group, "N2")
    second_group = await create(db, OverrideTarget.GROUP, group, "N3")
    async with db.session() as s:
        got = await override_level(
            s, content_id=built.content_id, group_ids=[built.group_id], account_id=account_id
        )
    assert got == "N3"  # group beats account; most recent group override first
    await create(db, OverrideTarget.CONTENT, content, "N0")
    async with db.session() as s:
        got = await override_level(
            s, content_id=built.content_id, group_ids=[built.group_id], account_id=account_id
        )
    assert got == "N0"  # content beats group
    # Revoked and expired overrides are ignored.
    async with db.transaction() as s:
        await s.execute(
            update(LevelOverride)
            .where(LevelOverride.target_type == "content")
            .values(revoked_at=now)
        )
        await s.execute(
            update(LevelOverride)
            .where(LevelOverride.public_id == second_group.override.public_id)
            .values(expires_at=now - timedelta(seconds=1))
        )
    async with db.session() as s:
        got = await override_level(
            s, content_id=built.content_id, group_ids=[built.group_id], account_id=account_id
        )
        assert got == "N2"
        far = await override_level(
            s,
            content_id=built.content_id,
            group_ids=[built.group_id],
            account_id=account_id,
            now=now + timedelta(days=60),
        )
    assert far is None
    assert first_group.override.level == "N2"
