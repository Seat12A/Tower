"""Manual merge and split of claim groups, representative recomputation (E3).

A group is "claims that say the same thing, verifiable by the same evidence": a merge
may never silently drop or contradict a verdict, a split never propagates one.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain import error_codes as ec
from tower.core.domain.errors import InvalidInput, NotFound
from tower.core.domain.factcheck.values import (
    UNVERIFIABLE_CLAIM_TYPES,
    ClaimGroupState,
    ClaimType,
    GroupEventKind,
    VerdictStatus,
)
from tower.core.domain.factcheck.vectors import medoid
from tower.core.domain.security import Actor, Permission
from tower.core.events.outbox import publish
from tower.core.factcheck import names
from tower.core.logging import get_logger
from tower.core.models.factcheck import (
    Claim,
    ClaimGroup,
    ClaimGroupMember,
    ContentClaim,
    Verdict,
)
from tower.core.models.ingestion import ContentItem
from tower.core.usecases.claims import journal_event, raise_group_level, request_investigation

__all__ = ["merge_groups", "recompute_representative", "split_group"]

log = get_logger(__name__)

MAX_MEDOID_MEMBERS = 200


async def _active_member_claims(session: AsyncSession, group_id: int) -> list[Claim]:
    return list(
        (
            await session.scalars(
                select(Claim)
                .join(ClaimGroupMember, ClaimGroupMember.claim_id == Claim.id)
                .where(
                    ClaimGroupMember.group_id == group_id,
                    ClaimGroupMember.removed_at.is_(None),
                )
                .order_by(Claim.id)
            )
        ).all()
    )


async def recompute_representative(
    session: AsyncSession, actor: Actor, group: ClaimGroup, *, reason: str | None = None
) -> bool:
    """Representative = medoid of the pivot vectors of the (at most 200 most recent)
    active members; a tie goes to the oldest claim. Returns True when it changed.
    Members without a vector are ignored; no vector at all keeps the representative."""
    members = await _active_member_claims(session, group.id)
    recent = sorted(members, key=lambda c: c.id, reverse=True)[:MAX_MEDOID_MEMBERS]
    candidates = sorted(
        (c for c in recent if (c.embedding_pivot or c.embedding) is not None),
        key=lambda c: c.id,
    )
    if not candidates:
        return False
    vectors = [c.embedding_pivot or c.embedding or [] for c in candidates]
    chosen = candidates[medoid(vectors)]
    chosen_vector = list(chosen.embedding_pivot or chosen.embedding or [])
    changed = chosen.id != group.representative_claim_id
    if changed:
        previous = group.representative_claim_id
        group.representative_claim_id = chosen.id
        await journal_event(
            session,
            actor,
            group,
            GroupEventKind.REPRESENTATIVE_CHANGED,
            claim_ids=[chosen.id],
            reason=reason,
            data={"previous_claim_id": previous},
        )
    group.representative_embedding = chosen_vector
    group.updated_at = func.now()
    await session.flush()
    return changed


async def _lock_groups(
    session: AsyncSession, *public_ids: uuid.UUID
) -> dict[uuid.UUID, ClaimGroup]:
    rows = (
        await session.scalars(
            select(ClaimGroup)
            .where(ClaimGroup.public_id.in_(public_ids))
            .order_by(ClaimGroup.id)
            .with_for_update()
        )
    ).all()
    found = {g.public_id: g for g in rows}
    for pid in public_ids:
        if pid not in found:
            raise NotFound("Claim group not found", code=ec.FCK_GROUP_NOT_FOUND, group=str(pid))
    return found


def _require_live(group: ClaimGroup) -> None:
    if group.state == ClaimGroupState.MERGED.value:
        raise InvalidInput(
            "Claim group already merged into another group",
            code=ec.FCK_GROUP_MERGED,
            group=str(group.public_id),
        )


async def _active_verdict(session: AsyncSession, group: ClaimGroup) -> Verdict | None:
    if group.active_verdict_id is None:
        return None
    verdict = await session.get(Verdict, group.active_verdict_id)
    if verdict is None or verdict.status != VerdictStatus.ACTIVE.value:
        return None
    return verdict


async def _move_members(
    session: AsyncSession,
    *,
    source: ClaimGroup,
    target: ClaimGroup,
    claim_ids: Sequence[int],
    reason: str,
) -> None:
    """Close the member rows of ``source`` and (re)open them in ``target``."""
    now = datetime.now(UTC)
    await session.execute(
        update(ClaimGroupMember)
        .where(
            ClaimGroupMember.group_id == source.id,
            ClaimGroupMember.claim_id.in_(list(claim_ids)),
            ClaimGroupMember.removed_at.is_(None),
        )
        .values(removed_at=now, reason=reason)
    )
    await session.flush()
    existing = set(
        await session.scalars(
            select(ClaimGroupMember.claim_id).where(
                ClaimGroupMember.group_id == target.id,
                ClaimGroupMember.claim_id.in_(list(claim_ids)),
            )
        )
    )
    for claim_id in claim_ids:
        if claim_id in existing:  # moved back (undo of an earlier move): reopen the row
            await session.execute(
                update(ClaimGroupMember)
                .where(
                    ClaimGroupMember.group_id == target.id, ClaimGroupMember.claim_id == claim_id
                )
                .values(removed_at=None, reason=None, added_at=now)
            )
        else:
            session.add(ClaimGroupMember(group_id=target.id, claim_id=claim_id))
    await session.flush()


async def merge_groups(
    session: AsyncSession,
    actor: Actor,
    *,
    target_id: uuid.UUID,
    source_id: uuid.UUID,
    reason: str,
) -> ClaimGroup:
    """Absorb ``source`` into ``target``.

    Allowed when the source has no active verdict, or both have one with the same
    veracity (the source verdict becomes ``superseded``). Only the source holding a
    verdict is ``TWR-FCK-023``; different veracities ``TWR-FCK-022``. The merge is undone
    by splitting the moved claims (ids in the ``merged`` journal row)."""
    actor.require(Permission.GROUPS_MANAGE)
    if target_id == source_id:
        raise InvalidInput("A group cannot be merged into itself", group=str(target_id))
    groups = await _lock_groups(session, target_id, source_id)
    target, source = groups[target_id], groups[source_id]
    _require_live(target)
    _require_live(source)

    target_verdict = await _active_verdict(session, target)
    source_verdict = await _active_verdict(session, source)
    if source_verdict is not None:
        if target_verdict is None:
            raise InvalidInput(
                "Only the target group of a merge may hold the active verdict",
                code=ec.FCK_MERGE_DIRECTION,
                target=str(target_id),
                source=str(source_id),
            )
        if target_verdict.veracity != source_verdict.veracity:
            raise InvalidInput(
                "Both groups hold active verdicts with different outcomes",
                code=ec.FCK_MERGE_CONFLICT,
                target=str(target_id),
                source=str(source_id),
            )
        source_verdict.status = VerdictStatus.SUPERSEDED.value
        source_verdict.updated_at = func.now()
        source.active_verdict_id = None

    # Drafts of the source never reach review: the target group carries the claims now.
    await session.execute(
        update(Verdict)
        .where(Verdict.claim_group_id == source.id, Verdict.status == VerdictStatus.DRAFT.value)
        .values(status=VerdictStatus.SUPERSEDED.value, updated_at=func.now())
    )

    members = await _active_member_claims(session, source.id)
    claim_ids = [c.id for c in members]
    await _move_members(session, source=source, target=target, claim_ids=claim_ids, reason=reason)
    target.reach = target.reach + source.reach
    source.state = ClaimGroupState.MERGED.value
    source.merged_into_id = target.id
    source.updated_at = func.now()
    await session.flush()
    await raise_group_level(session, actor, target, source.level, reason="content")
    await recompute_representative(session, actor, target, reason=reason)

    data = {
        "target_group_id": str(target.public_id),
        "source_group_id": str(source.public_id),
        "superseded_verdict_id": str(source_verdict.public_id) if source_verdict else None,
    }
    for group, other in ((target, source), (source, target)):
        await journal_event(
            session,
            actor,
            group,
            GroupEventKind.MERGED,
            other_group_id=other.id,
            claim_ids=claim_ids,
            reason=reason,
            data=data,
        )
    await publish(
        session,
        names.CLAIM_GROUP_MERGED,
        {
            "target_group_id": str(target.public_id),
            "source_group_id": str(source.public_id),
            "claim_ids": [str(c.public_id) for c in members],
            "actor": actor.label,
            "reason": reason,
        },
        aggregate_type="claim_group",
        aggregate_id=str(target.public_id),
        actor=actor.label,
    )
    log.info("factcheck.groups_merged", target=target.id, source=source.id, claims=len(claim_ids))
    return target


async def _latest_content(session: AsyncSession, claim_id: int) -> ContentItem | None:
    return await session.scalar(
        select(ContentItem)
        .join(ContentClaim, ContentClaim.content_id == ContentItem.id)
        .where(ContentClaim.claim_id == claim_id)
        .order_by(ContentItem.created_at_platform.desc().nulls_last(), ContentItem.id.desc())
        .limit(1)
    )


async def split_group(
    session: AsyncSession,
    actor: Actor,
    *,
    group_id: uuid.UUID,
    claim_ids: Sequence[uuid.UUID],
    reason: str,
) -> ClaimGroup:
    """Move ``claim_ids`` into a NEW group (no verdict, state ``queued``, same level).

    The source keeps its verdict; the moved claims are no longer covered by it, so an
    investigation is requested for the new group when a moved claim is verifiable."""
    actor.require(Permission.GROUPS_MANAGE)
    group = (await _lock_groups(session, group_id))[group_id]
    _require_live(group)
    wanted = list(dict.fromkeys(claim_ids))
    if not wanted:
        raise InvalidInput("No claim to split", code=ec.FCK_SPLIT_INVALID, group=str(group_id))
    members = await _active_member_claims(session, group.id)
    by_public = {c.public_id: c for c in members}
    unknown = [str(p) for p in wanted if p not in by_public]
    if unknown:
        raise InvalidInput(
            "Claims are not active members of the group",
            code=ec.FCK_SPLIT_INVALID,
            group=str(group_id),
            claims=unknown,
        )
    moved = sorted((by_public[p] for p in wanted), key=lambda c: c.id)
    if len(moved) >= len(members):
        raise InvalidInput(
            "A split must leave at least one claim in the group",
            code=ec.FCK_SPLIT_INVALID,
            group=str(group_id),
        )

    new = ClaimGroup(
        public_id=uuid.uuid4(),
        representative_claim_id=moved[0].id,
        level=group.level,
        state=ClaimGroupState.QUEUED.value,
        reach=0,
        representative_embedding=moved[0].embedding_pivot or moved[0].embedding,
    )
    session.add(new)
    await session.flush()
    moved_ids = [c.id for c in moved]
    await _move_members(session, source=group, target=new, claim_ids=moved_ids, reason=reason)
    await recompute_representative(session, actor, new, reason=reason)
    if group.representative_claim_id in moved_ids:
        await recompute_representative(session, actor, group, reason=reason)
    group.updated_at = func.now()

    for subject, other in ((group, new), (new, group)):
        await journal_event(
            session,
            actor,
            subject,
            GroupEventKind.SPLIT,
            other_group_id=other.id,
            claim_ids=moved_ids,
            reason=reason,
            data={"source_group_id": str(group.public_id), "new_group_id": str(new.public_id)},
        )
    if any(ClaimType(c.claim_type) not in UNVERIFIABLE_CLAIM_TYPES for c in moved):
        content = await _latest_content(session, new.representative_claim_id)
        if content is not None:
            await request_investigation(
                session,
                actor,
                group=new,
                content=content,
                level=new.level,
                requested_by=actor.label,
                flags={"split_from": str(group.public_id)},
            )
    await publish(
        session,
        names.CLAIM_GROUP_SPLIT,
        {
            "group_id": str(group.public_id),
            "new_group_id": str(new.public_id),
            "claim_ids": [str(c.public_id) for c in moved],
            "actor": actor.label,
            "reason": reason,
        },
        aggregate_type="claim_group",
        aggregate_id=str(group.public_id),
        actor=actor.label,
    )
    log.info("factcheck.group_split", group=group.id, new_group=new.id, claims=len(moved_ids))
    return new
