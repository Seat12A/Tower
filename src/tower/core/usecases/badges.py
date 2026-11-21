"""Verification badges: grant, revoke, block, public lookup, history, organisation keys.

Affiliations live in ``badge_affiliations.py``, requests and reports in
``badge_submissions.py``, automatic sources (Wikidata, X) in ``badge_sources.py``.
Every write records a ``badge_events`` row and publishes an event in the same
transaction. See docs/badges.md.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from tower.core.auth.tokens import generate_org_token, hash_token
from tower.core.domain import error_codes as ec
from tower.core.domain.badges import (
    AFFILIABLE_CATEGORIES,
    MAX_LABEL,
    MAX_NOTE,
    AccountRef,
    AffiliationStatus,
    BadgeAction,
    BadgeCategory,
    BadgeSource,
    BadgeStatus,
    check_expertise,
    check_text,
    check_url,
    color_dark_of,
    color_of,
    normalize_category,
    parse_category,
    parse_lookup_ids,
    stored_values,
)
from tower.core.domain.errors import Conflict, NotFound
from tower.core.domain.security import Actor, Permission
from tower.core.events.outbox import publish
from tower.core.models.badges import Badge, BadgeAffiliation, BadgeEvent, BadgeOrgKey
from tower.core.models.ingestion import PlatformAccount

AGGREGATE = "badge"
AUTO_PREFIX = "system:"
"""Actor label prefix of automatic revocations: those may be undone by a source."""


MAX_LOOKUP_AFFILIATIONS = 5
"""Affiliations returned per account by the lookup (primary first)."""


def utcnow() -> datetime:
    return datetime.now(UTC)


def required[T](value: T | None, what: str) -> T:
    """Value guaranteed by a foreign key; a missing one is a data integrity error."""
    if value is None:
        raise RuntimeError(f"missing {what}")
    return value


# --- Accounts --------------------------------------------------------------------------


async def ensure_account(
    session: AsyncSession, ref: AccountRef, *, handle: str | None = None
) -> PlatformAccount:
    """Account of ``ref`` (created if unknown). The handle is informative only."""
    stmt = insert(PlatformAccount).values(
        platform=ref.platform.value, external_id=ref.external_id, handle=handle
    )
    account = (
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[PlatformAccount.platform, PlatformAccount.external_id],
                set_={"handle": func.coalesce(stmt.excluded.handle, PlatformAccount.handle)},
            ).returning(PlatformAccount)
        )
    ).scalar_one()
    return account


async def find_account(session: AsyncSession, ref: AccountRef) -> PlatformAccount | None:
    return await session.scalar(
        select(PlatformAccount).where(
            PlatformAccount.platform == ref.platform.value,
            PlatformAccount.external_id == ref.external_id,
        )
    )


def account_ref_payload(account: PlatformAccount) -> dict[str, Any]:
    return {"platform": account.platform, "external_id": account.external_id}


# --- Payloads and history -------------------------------------------------------------


def badge_payload(
    badge: Badge, account: PlatformAccount, *, internal: bool = True
) -> dict[str, Any]:
    """API and event representation (ids as strings). ``internal`` adds team fields."""
    out: dict[str, Any] = {
        "public_id": str(badge.public_id),
        "account": account_ref_payload(account),
        "status": badge.status,
        "category": normalize_category(badge.category),
        "color": color_of(badge.category) if badge.category else None,
        "color_dark": color_dark_of(badge.category) if badge.category else None,
        "label": badge.label,
        "logo_url": badge.logo_url,
        "expertise": badge.expertise,
    }
    if internal:
        out.update(
            {
                "handle": account.handle,
                "source": badge.source,
                "wikidata_qid": badge.wikidata_qid,
                "note": badge.note,
                "granted_by": badge.granted_by,
                "granted_at": badge.granted_at.isoformat() if badge.granted_at else None,
                "ended_by": badge.ended_by,
                "ended_at": badge.ended_at.isoformat() if badge.ended_at else None,
                "end_reason": badge.end_reason,
                "version": badge.version,
            }
        )
    return out


async def record(
    session: AsyncSession,
    *,
    account_id: int,
    action: BadgeAction,
    actor: Actor,
    badge_id: int | None = None,
    affiliation_id: int | None = None,
    source: str | None = None,
    reason: str | None = None,
    changes: dict[str, Any] | None = None,
) -> None:
    """Append one history row (``badge_events``)."""
    session.add(
        BadgeEvent(
            account_id=account_id,
            badge_id=badge_id,
            affiliation_id=affiliation_id,
            action=action.value,
            actor=actor.label,
            source=source,
            reason=reason,
            changes=changes or {},
        )
    )


async def _publish_badge(
    session: AsyncSession, actor: Actor, action: BadgeAction, badge: Badge, account: PlatformAccount
) -> None:
    await publish(
        session,
        "badge.changed",
        {"action": action.value, "badge": badge_payload(badge, account)},
        aggregate_type=AGGREGATE,
        aggregate_id=str(badge.public_id),
        actor=actor.label,
    )


async def _badge_for_update(session: AsyncSession, account_id: int) -> Badge | None:
    return await session.scalar(
        select(Badge).where(Badge.account_id == account_id).with_for_update()
    )


async def badge_of(session: AsyncSession, ref: AccountRef) -> tuple[Badge, PlatformAccount]:
    """Badge row and account of ``ref`` (any status) or ``NotFound``."""
    row = (
        await session.execute(
            select(Badge, PlatformAccount)
            .join(PlatformAccount, PlatformAccount.id == Badge.account_id)
            .where(
                PlatformAccount.platform == ref.platform.value,
                PlatformAccount.external_id == ref.external_id,
            )
        )
    ).first()
    if row is None:
        raise NotFound(
            "No badge for this account",
            code=ec.BDG_NOT_FOUND,
            platform=ref.platform.value,
            external_id=ref.external_id,
        )
    return row[0], row[1]


async def active_org_badge(
    session: AsyncSession, ref: AccountRef, *, lock: bool = False
) -> tuple[Badge, PlatformAccount]:
    """Active affiliable badge of an organisation account, else ``TWR-BDG-006``."""
    stmt = (
        select(Badge, PlatformAccount)
        .join(PlatformAccount, PlatformAccount.id == Badge.account_id)
        .where(
            PlatformAccount.platform == ref.platform.value,
            PlatformAccount.external_id == ref.external_id,
        )
    )
    if lock:
        stmt = stmt.with_for_update(of=Badge)
    row = (await session.execute(stmt)).first()
    if row is None or not is_affiliable(row[0]):
        raise Conflict(
            "Organisation has no affiliable badge",
            code=ec.BDG_NOT_AFFILIABLE,
            platform=ref.platform.value,
            external_id=ref.external_id,
        )
    return row[0], row[1]


def set_category(badge: Badge, category: BadgeCategory, change: Any) -> None:
    """Set the category through ``change`` (recorded), unless only the stored spelling
    differs (legacy value): then rewrite it silently, without history or source change."""
    if normalize_category(badge.category) != category.value:
        change("category", category.value)
    else:
        badge.category = category.value


def is_affiliable(badge: Badge) -> bool:
    return (
        badge.status == BadgeStatus.ACTIVE.value
        and badge.category is not None
        and BadgeCategory(badge.category) in AFFILIABLE_CATEGORIES
    )


# --- Staff operations -----------------------------------------------------------------


async def grant_badge(
    session: AsyncSession,
    actor: Actor,
    ref: AccountRef,
    *,
    category: str,
    label: str | None = None,
    logo_url: str | None = None,
    expertise: str | None = None,
    note: str | None = None,
    handle: str | None = None,
    version: int | None = None,
    source: BadgeSource = BadgeSource.MANUAL,
) -> tuple[Badge, PlatformAccount]:
    """Grant (or update) the badge of an account. Refused on a blocked account.

    ``version``: optimistic lock on an existing row (``TWR-BDG-005``). Passing ``""`` for
    ``label``, ``logo_url``, ``expertise`` or ``note`` clears the field; ``None`` keeps
    it. ``expertise`` is only allowed on an expert badge (``TWR-BDG-027``) and is
    cleared when the category is no longer expert.
    """
    actor.require(Permission.BADGES_MANAGE)
    cat = parse_category(category)
    new_expertise = check_expertise(cat, expertise)
    new_label = check_text(label, field="label", max_chars=MAX_LABEL)
    new_logo = check_url(logo_url, field="logo_url")
    new_note = check_text(note, field="note", max_chars=MAX_NOTE)
    account = await ensure_account(session, ref, handle=handle)
    badge = await _badge_for_update(session, account.id)
    now = utcnow()
    if badge is not None and version is not None and badge.version != version:
        raise Conflict(
            "Badge was modified in the meantime",
            code=ec.BDG_VERSION_CONFLICT,
            expected=version,
            current=badge.version,
        )
    if badge is not None and badge.status == BadgeStatus.BLOCKED.value:
        raise Conflict(
            "Account is blocked from badges",
            code=ec.BDG_ACCOUNT_BLOCKED,
            platform=ref.platform.value,
            external_id=ref.external_id,
        )
    if badge is None:
        badge = Badge(account_id=account.id, status=BadgeStatus.REVOKED.value)
        session.add(badge)
        await session.flush()
    was_active = badge.status == BadgeStatus.ACTIVE.value
    was_affiliable = is_affiliable(badge)
    changes: dict[str, Any] = {}

    def change(field: str, new: Any) -> None:
        old = getattr(badge, field)
        if old != new:
            changes[field] = {"from": old, "to": new}
            setattr(badge, field, new)

    set_category(badge, cat, change)
    if label is not None:
        change("label", new_label)
    if logo_url is not None:
        change("logo_url", new_logo)
    if cat is not BadgeCategory.EXPERT:
        change("expertise", None)
    elif expertise is not None:
        change("expertise", new_expertise)
    if note is not None:
        change("note", new_note)
    if not was_active:
        change("status", BadgeStatus.ACTIVE.value)
        change("source", source.value)
        badge.granted_by = actor.label
        badge.granted_at = now
        badge.ended_by = badge.ended_at = badge.end_reason = None
    elif "category" in changes:
        # A team decision on the category makes the badge manual (sources stop changing it).
        change("source", source.value)
    if not changes:
        return badge, account
    badge.version += 1
    badge.updated_at = now
    await session.flush()
    action = BadgeAction.UPDATED if was_active else BadgeAction.GRANTED
    await record(
        session,
        account_id=account.id,
        badge_id=badge.id,
        action=action,
        actor=actor,
        source=badge.source,
        changes=changes,
    )
    await _publish_badge(session, actor, action, badge, account)
    if was_affiliable and not is_affiliable(badge):
        from tower.core.usecases.badge_affiliations import end_org_affiliations

        await end_org_affiliations(session, actor, badge, reason="org_category_changed")
    return badge, account


async def revoke_badge(
    session: AsyncSession, actor: Actor, ref: AccountRef, *, reason: str | None = None
) -> tuple[Badge, PlatformAccount]:
    """Revoke an active badge. A team revocation is sticky: automatic sources (Wikidata,
    X) never grant it back. The organisation's affiliations all end."""
    actor.require(Permission.BADGES_MANAGE)
    badge, account = await badge_of(session, ref)
    await _lock(session, badge)
    if badge.status != BadgeStatus.ACTIVE.value:
        raise Conflict("Badge is not active", code=ec.BDG_NOT_FOUND, status=badge.status)
    await end_badge(session, actor, badge, account, status=BadgeStatus.REVOKED, reason=reason)
    return badge, account


async def end_badge(
    session: AsyncSession,
    actor: Actor,
    badge: Badge,
    account: PlatformAccount,
    *,
    status: BadgeStatus,
    reason: str | None,
) -> None:
    """Revoke or block (internal, no permission check): history, event, affiliations."""
    reason = check_text(reason, field="reason", max_chars=MAX_NOTE)
    was_affiliable = is_affiliable(badge)
    old_status = badge.status
    badge.status = status.value
    badge.ended_by = actor.label
    badge.ended_at = utcnow()
    badge.end_reason = reason
    badge.version += 1
    badge.updated_at = badge.ended_at
    await session.flush()
    action = BadgeAction.BLOCKED if status is BadgeStatus.BLOCKED else BadgeAction.REVOKED
    await record(
        session,
        account_id=account.id,
        badge_id=badge.id,
        action=action,
        actor=actor,
        source=badge.source,
        reason=reason,
        changes={"status": {"from": old_status, "to": status.value}},
    )
    await _publish_badge(session, actor, action, badge, account)
    if was_affiliable:
        from tower.core.usecases.badge_affiliations import end_org_affiliations

        await end_org_affiliations(session, actor, badge, reason="org_badge_revoked")


async def block_account(
    session: AsyncSession,
    actor: Actor,
    ref: AccountRef,
    *,
    reason: str | None = None,
    handle: str | None = None,
) -> tuple[Badge, PlatformAccount]:
    """Block an account from badges: revokes the badge, ends its own affiliations, and no
    source (Wikidata, X, requests) can give it a badge until ``unblock_account``."""
    actor.require(Permission.BADGES_MANAGE)
    account = await ensure_account(session, ref, handle=handle)
    badge = await _badge_for_update(session, account.id)
    if badge is not None and badge.status == BadgeStatus.BLOCKED.value:
        return badge, account
    if badge is None:
        badge = Badge(account_id=account.id, status=BadgeStatus.REVOKED.value)
        session.add(badge)
        await session.flush()
    await end_badge(session, actor, badge, account, status=BadgeStatus.BLOCKED, reason=reason)
    from tower.core.usecases.badge_affiliations import end_member_affiliations

    await end_member_affiliations(session, actor, account.id, reason="account_blocked")
    return badge, account


async def unblock_account(
    session: AsyncSession, actor: Actor, ref: AccountRef
) -> tuple[Badge, PlatformAccount]:
    """Lift a block. The account has no badge afterwards (a new grant is needed)."""
    actor.require(Permission.BADGES_MANAGE)
    badge, account = await badge_of(session, ref)
    await _lock(session, badge)
    if badge.status != BadgeStatus.BLOCKED.value:
        raise Conflict("Account is not blocked", code=ec.BDG_NOT_FOUND, status=badge.status)
    badge.status = BadgeStatus.REVOKED.value
    badge.version += 1
    badge.updated_at = utcnow()
    await session.flush()
    await record(
        session,
        account_id=account.id,
        badge_id=badge.id,
        action=BadgeAction.UNBLOCKED,
        actor=actor,
        changes={"status": {"from": "blocked", "to": "revoked"}},
    )
    await _publish_badge(session, actor, BadgeAction.UNBLOCKED, badge, account)
    return badge, account


async def _lock(session: AsyncSession, badge: Badge) -> None:
    await session.execute(select(Badge.id).where(Badge.id == badge.id).with_for_update())
    await session.refresh(badge)


# --- Automatic sources (internal, called by badge_sources) ---------------------------


@dataclass(frozen=True, slots=True)
class AutoGrant:
    category: BadgeCategory
    source: BadgeSource
    label: str | None = None
    logo_url: str | None = None
    expertise: str | None = None
    """Field of expertise (expert category only; ignored otherwise)."""
    wikidata_qid: str | None = None


async def apply_automatic(
    session: AsyncSession, actor: Actor, account: PlatformAccount, grant: AutoGrant
) -> BadgeAction | None:
    """Apply what an automatic source says about one account. Returns the action taken.

    - no row, or revoked automatically → granted;
    - blocked, or revoked by the team → nothing (the team's decision wins);
    - active from the same automatic source → category, label and logo follow the source;
    - active X government badge → kept (Wikidata only fills its identifiers);
    - active from a Wikidata import, X says government → becomes X government;
    - active manual or request badge → category kept; only Wikidata identifiers filled.
    A Wikidata item whose QID differs from the one already linked is ignored (safety).
    """
    actor.require(Permission.BADGES_MANAGE)
    badge = await _badge_for_update(session, account.id)
    now = utcnow()
    if (
        badge is not None
        and grant.wikidata_qid
        and badge.wikidata_qid
        not in (
            None,
            grant.wikidata_qid,
        )
    ):
        return None
    if badge is not None and grant.source is BadgeSource.WIKIDATA:
        badge.wikidata_qid = grant.wikidata_qid
        badge.wikidata_seen_at = now
    if badge is not None and badge.status == BadgeStatus.BLOCKED.value:
        return None
    if (
        badge is not None
        and badge.status == BadgeStatus.REVOKED.value
        and badge.ended_by is not None
        and not badge.ended_by.startswith(AUTO_PREFIX)
    ):
        return None
    if badge is None:
        badge = Badge(account_id=account.id, status=BadgeStatus.REVOKED.value)
        if grant.source is BadgeSource.WIKIDATA:
            badge.wikidata_qid = grant.wikidata_qid
            badge.wikidata_seen_at = now
        session.add(badge)
        await session.flush()

    changes: dict[str, Any] = {}

    def change(field: str, new: Any) -> None:
        old = getattr(badge, field)
        if old != new:
            changes[field] = {"from": old, "to": new}
            setattr(badge, field, new)

    if badge.status != BadgeStatus.ACTIVE.value:
        was_affiliable = False
        action = BadgeAction.GRANTED
        change("status", BadgeStatus.ACTIVE.value)
        change("category", grant.category.value)
        change("source", grant.source.value)
        if grant.label is not None:
            change("label", grant.label)
        if grant.logo_url is not None:
            change("logo_url", grant.logo_url)
        if grant.category is BadgeCategory.EXPERT:
            if grant.expertise is not None:
                change("expertise", grant.expertise)
        else:
            change("expertise", None)
        badge.granted_by = actor.label
        badge.granted_at = now
        badge.ended_by = badge.ended_at = badge.end_reason = None
    else:
        was_affiliable = is_affiliable(badge)
        action = BadgeAction.UPDATED
        current = BadgeSource(badge.source) if badge.source else BadgeSource.MANUAL
        if current is grant.source:
            set_category(badge, grant.category, change)
        elif current is BadgeSource.WIKIDATA and grant.source is BadgeSource.X_GOVERNMENT:
            set_category(badge, grant.category, change)
            change("source", grant.source.value)
        if badge.label is None and grant.label is not None:
            change("label", grant.label)
        if badge.logo_url is None and grant.logo_url is not None:
            change("logo_url", grant.logo_url)
        if normalize_category(badge.category) != BadgeCategory.EXPERT.value:
            change("expertise", None)
        elif badge.expertise is None and grant.expertise is not None:
            change("expertise", grant.expertise)
    if not changes:
        await session.flush()
        return None
    badge.version += 1
    badge.updated_at = now
    await session.flush()
    await record(
        session,
        account_id=account.id,
        badge_id=badge.id,
        action=action,
        actor=actor,
        source=grant.source.value,
        changes=changes,
    )
    await _publish_badge(session, actor, action, badge, account)
    if was_affiliable and not is_affiliable(badge):
        from tower.core.usecases.badge_affiliations import end_org_affiliations

        await end_org_affiliations(session, actor, badge, reason="org_category_changed")
    return action


async def revoke_automatic(
    session: AsyncSession, actor: Actor, badge: Badge, *, reason: str
) -> bool:
    """System revocation of an automatic badge (source no longer confirms it)."""
    actor.require(Permission.BADGES_MANAGE)
    await _lock(session, badge)
    if badge.status != BadgeStatus.ACTIVE.value or not (
        badge.source and BadgeSource(badge.source).automatic
    ):
        return False
    account = await session.get(PlatformAccount, badge.account_id)
    if account is None:  # pragma: no cover - FK
        return False
    await end_badge(session, actor, badge, account, status=BadgeStatus.REVOKED, reason=reason)
    return True


# --- Reads ----------------------------------------------------------------------------


async def list_badges(
    session: AsyncSession,
    actor: Actor,
    *,
    status: str | None = "active",
    category: str | None = None,
    source: str | None = None,
    platform: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> Sequence[tuple[Badge, PlatformAccount]]:
    actor.require(Permission.BADGES_READ)
    stmt = (
        select(Badge, PlatformAccount)
        .join(PlatformAccount, PlatformAccount.id == Badge.account_id)
        .order_by(Badge.updated_at.desc(), Badge.id.desc())
        .limit(min(max(limit, 1), 500))
        .offset(max(offset, 0))
    )
    if status is not None:
        stmt = stmt.where(Badge.status == BadgeStatus(status).value)
    if category is not None:
        stmt = stmt.where(Badge.category.in_(stored_values(parse_category(category))))
    if source is not None:
        stmt = stmt.where(Badge.source == BadgeSource(source).value)
    if platform is not None:
        stmt = stmt.where(PlatformAccount.platform == platform)
    return [(b, a) for b, a in (await session.execute(stmt)).all()]


async def get_badge(
    session: AsyncSession, actor: Actor, ref: AccountRef
) -> tuple[Badge, PlatformAccount]:
    actor.require(Permission.BADGES_READ)
    return await badge_of(session, ref)


async def badge_history(
    session: AsyncSession, actor: Actor, ref: AccountRef, *, limit: int = 100
) -> list[dict[str, Any]]:
    """History of an account (its badge and its affiliations as a member), newest first.

    Organisation side: events of its affiliations are on the member accounts."""
    actor.require(Permission.BADGES_READ)
    account = await find_account(session, ref)
    if account is None:
        raise NotFound("Unknown account", code=ec.BDG_NOT_FOUND, external_id=ref.external_id)
    rows = (
        await session.scalars(
            select(BadgeEvent)
            .where(BadgeEvent.account_id == account.id)
            .order_by(BadgeEvent.id.desc())
            .limit(min(max(limit, 1), 500))
        )
    ).all()
    return [
        {
            "action": e.action,
            "actor": e.actor,
            "source": e.source,
            "reason": e.reason,
            "changes": e.changes,
            "created_at": e.created_at.isoformat(),
        }
        for e in rows
    ]


async def lookup_badges(
    session: AsyncSession, actor: Actor, platform: str, ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """Public batch lookup for the extension: native id → what to display.

    Only accounts with something to show are returned (absent = no Seat 12A badge).
    Badge and affiliations are independent: ``category`` and ``color`` (the check mark)
    come from the account's own badge only (None without one); ``affiliation`` is the
    primary affiliation, whose organisation logo goes next to the handle; ``affiliations``
    lists the active ones, primary first (at most ``MAX_LOOKUP_AFFILIATIONS``).
    Two SQL queries whatever the batch size (≤ 100 ids).
    """
    actor.require(Permission.BADGES_READ_PUBLIC)
    plat, clean = parse_lookup_ids(platform, ids)
    accounts = (
        await session.execute(
            select(PlatformAccount.id, PlatformAccount.external_id, Badge)
            .outerjoin(
                Badge,
                and_(
                    Badge.account_id == PlatformAccount.id,
                    Badge.status == BadgeStatus.ACTIVE.value,
                ),
            )
            .where(PlatformAccount.platform == plat.value, PlatformAccount.external_id.in_(clean))
        )
    ).all()
    if not accounts:
        return {}
    org = aliased(Badge)
    org_account = aliased(PlatformAccount)
    affs: dict[int, list[dict[str, Any]]] = {}
    rows = (
        await session.execute(
            select(BadgeAffiliation.member_account_id, BadgeAffiliation.title, org, org_account)
            .join(
                org,
                and_(
                    org.id == BadgeAffiliation.org_badge_id,
                    org.status == BadgeStatus.ACTIVE.value,
                    org.category.in_([v for c in AFFILIABLE_CATEGORIES for v in stored_values(c)]),
                ),
            )
            .join(org_account, org_account.id == org.account_id)
            .where(
                BadgeAffiliation.member_account_id.in_([a[0] for a in accounts]),
                BadgeAffiliation.status == AffiliationStatus.ACTIVE.value,
                BadgeAffiliation.expires_at > utcnow(),
            )
            .order_by(
                BadgeAffiliation.member_account_id,
                BadgeAffiliation.is_primary.desc(),
                BadgeAffiliation.validated_at,
                BadgeAffiliation.id,
            )
        )
    ).all()
    for member_id, title, o, oa in rows:
        listed = affs.setdefault(member_id, [])
        if len(listed) < MAX_LOOKUP_AFFILIATIONS:
            listed.append(
                {
                    "org": {
                        "platform": oa.platform,
                        "external_id": oa.external_id,
                        "category": normalize_category(o.category),
                        "color": color_of(o.category or ""),
                        "color_dark": color_dark_of(o.category or ""),
                        "label": o.label,
                        "logo_url": o.logo_url,
                    },
                    "title": title,
                }
            )
    out: dict[str, dict[str, Any]] = {}
    for account_id, xid, own in accounts:
        listed = affs.get(account_id, [])
        if xid is None or (own is None and not listed):
            continue
        badge = (
            {
                "category": normalize_category(own.category),
                "color": color_of(own.category),
                "color_dark": color_dark_of(own.category),
                "label": own.label,
                "expertise": own.expertise,
            }
            if own is not None and own.category
            else None
        )
        out[xid] = {
            "category": badge["category"] if badge else None,
            "color": badge["color"] if badge else None,
            "color_dark": badge["color_dark"] if badge else None,
            "badge": badge,
            "affiliation": listed[0] if listed else None,
            "affiliations": listed,
        }
    return out


async def account_badge_details(
    session: AsyncSession, actor: Actor, ref: AccountRef
) -> dict[str, Any]:
    """Public details of one account: its badge and all its active affiliations."""
    actor.require(Permission.BADGES_READ_PUBLIC)
    account = await find_account(session, ref)
    badge = None
    affiliations: list[dict[str, Any]] = []
    if account is not None:
        row = await session.scalar(
            select(Badge).where(Badge.account_id == account.id, Badge.status == "active")
        )
        if row is not None:
            badge = badge_payload(row, account, internal=False)
        org = aliased(Badge)
        org_account = aliased(PlatformAccount)
        rows = (
            await session.execute(
                select(BadgeAffiliation, org, org_account)
                .join(org, org.id == BadgeAffiliation.org_badge_id)
                .join(org_account, org_account.id == org.account_id)
                .where(
                    BadgeAffiliation.member_account_id == account.id,
                    BadgeAffiliation.status == AffiliationStatus.ACTIVE.value,
                    BadgeAffiliation.expires_at > utcnow(),
                    org.status == "active",
                )
                .order_by(BadgeAffiliation.is_primary.desc(), BadgeAffiliation.validated_at)
            )
        ).all()
        affiliations = [
            {
                "primary": a.is_primary,
                "title": a.title,
                "org": badge_payload(o, oa, internal=False),
                "expires_at": a.expires_at.isoformat() if a.expires_at else None,
            }
            for a, o, oa in rows
            if is_affiliable(o)
        ]
    if badge is None and not affiliations:
        raise NotFound(
            "No badge for this account",
            code=ec.BDG_NOT_FOUND,
            platform=ref.platform.value,
            external_id=ref.external_id,
        )
    return {
        "account": {"platform": ref.platform.value, "external_id": ref.external_id},
        "badge": badge,
        "affiliations": affiliations,
    }


# --- Organisation keys ---------------------------------------------------------------


async def create_org_key(
    session: AsyncSession, actor: Actor, org_ref: AccountRef, *, label: str | None = None
) -> tuple[BadgeOrgKey, str]:
    """New organisation key; the token is returned once and only its hash is stored."""
    actor.require(Permission.BADGES_MANAGE)
    label = check_text(label, field="label", max_chars=MAX_LABEL)
    badge, account = await active_org_badge(session, org_ref)
    token = generate_org_token()
    key = BadgeOrgKey(
        org_badge_id=badge.id, token_hash=hash_token(token), label=label, created_by=actor.label
    )
    session.add(key)
    await session.flush()
    await session.refresh(key)
    await record(
        session,
        account_id=account.id,
        badge_id=badge.id,
        action=BadgeAction.ORG_KEY_CREATED,
        actor=actor,
        changes={"key": str(key.public_id), "label": label},
    )
    return key, token


async def list_org_keys(
    session: AsyncSession, actor: Actor, org_ref: AccountRef
) -> Sequence[BadgeOrgKey]:
    actor.require(Permission.BADGES_READ)
    badge, _ = await badge_of(session, org_ref)
    return (
        await session.scalars(
            select(BadgeOrgKey)
            .where(BadgeOrgKey.org_badge_id == badge.id)
            .order_by(BadgeOrgKey.id.desc())
        )
    ).all()


async def revoke_org_key(session: AsyncSession, actor: Actor, key_public_id: str) -> BadgeOrgKey:
    actor.require(Permission.BADGES_MANAGE)
    try:
        public_id = uuid.UUID(key_public_id)
    except ValueError:
        raise NotFound("Organisation key not found", code=ec.BDG_ORG_KEY_NOT_FOUND) from None
    key = await session.scalar(
        select(BadgeOrgKey).where(BadgeOrgKey.public_id == public_id).with_for_update()
    )
    if key is None:
        raise NotFound("Organisation key not found", code=ec.BDG_ORG_KEY_NOT_FOUND)
    if key.revoked_at is None:
        key.revoked_at = utcnow()
        badge = await session.get(Badge, key.org_badge_id)
        if badge is not None:
            await record(
                session,
                account_id=badge.account_id,
                badge_id=badge.id,
                action=BadgeAction.ORG_KEY_REVOKED,
                actor=actor,
                changes={"key": str(key.public_id)},
            )
        await session.flush()
    return key


async def org_badge_of_actor(session: AsyncSession, actor: Actor) -> tuple[Badge, PlatformAccount]:
    """Badge of the organisation behind an organisation actor (must still be affiliable)."""
    actor.require(Permission.BADGES_ORG_MANAGE)
    try:
        public_id = uuid.UUID(actor.id or "")
    except ValueError:
        raise NotFound("Organisation not found", code=ec.BDG_NOT_FOUND) from None
    row = (
        await session.execute(
            select(Badge, PlatformAccount)
            .join(PlatformAccount, PlatformAccount.id == Badge.account_id)
            .where(Badge.public_id == public_id)
        )
    ).first()
    if row is None or not is_affiliable(row[0]):
        raise Conflict("Organisation has no affiliable badge", code=ec.BDG_NOT_AFFILIABLE)
    return row[0], row[1]


def parse_ref(platform: str, external_id: str) -> AccountRef:
    return AccountRef.parse(platform, external_id)


def require_any(actor: Actor, *permissions: Permission) -> None:
    """At least one of ``permissions`` (the first one is named in the error)."""
    if not any(actor.can(p) for p in permissions):
        actor.require(permissions[0])


__all__ = [
    "AutoGrant",
    "account_badge_details",
    "active_org_badge",
    "apply_automatic",
    "badge_history",
    "badge_of",
    "badge_payload",
    "block_account",
    "create_org_key",
    "ensure_account",
    "find_account",
    "get_badge",
    "grant_badge",
    "list_badges",
    "list_org_keys",
    "lookup_badges",
    "org_badge_of_actor",
    "revoke_automatic",
    "revoke_badge",
    "revoke_org_key",
    "unblock_account",
]
