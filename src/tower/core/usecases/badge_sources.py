"""Automatic badge sources: Wikidata import and X government verification.

Both go through ``badges.apply_automatic``: a team decision (manual grant, revocation,
block) always wins over a source. Called by the ``badges`` hangar service with a system
actor; the X hint endpoint is public (extension).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain.badges import (
    AFFILIABLE_CATEGORIES,
    AccountRef,
    BadgeCategory,
    BadgeSource,
    BadgeStatus,
    classify_wikidata,
    commons_file_url,
    parse_lookup_ids,
)
from tower.core.domain.errors import InvalidInput
from tower.core.domain.platforms import Platform, validate_external_id
from tower.core.domain.security import Actor, Permission
from tower.core.events.outbox import publish
from tower.core.models.badges import Badge, BadgeImportRun, BadgeWikidataLink, BadgeXCheck
from tower.core.models.ingestion import PlatformAccount
from tower.core.ports.badges import WikidataAccount, WikidataItem
from tower.core.ports.platforms import PublicAccount
from tower.core.usecases.badges import (
    AutoGrant,
    apply_automatic,
    ensure_account,
    revoke_automatic,
    utcnow,
)

QUEUE = "badges"
SCOPE_JOB = "badges.wikidata_scope"
BATCH_JOB = "badges.wikidata_batch"
X_JOB = "badges.x_verify"
BATCH_SIZE = 200

_PROPOSE_SQL = text(
    """
WITH ins AS (
  INSERT INTO badge_affiliations
    (member_account_id, org_badge_id, status, source, wikidata_property, requested_by)
  SELECT DISTINCT ON (l.member_account_id, b.id)
         l.member_account_id, b.id, 'pending', 'wikidata', l.property, :actor
  FROM badge_wikidata_links l
  JOIN badges b ON b.wikidata_qid = l.org_qid AND b.status = 'active'
                AND b.category = ANY(:affiliable)
  LEFT JOIN badges mb ON mb.account_id = l.member_account_id
  WHERE (l.member_account_id = ANY(:members) OR l.org_qid = ANY(:org_qids))
    AND b.account_id <> l.member_account_id
    AND (mb.status IS NULL OR mb.status <> 'blocked')
  ORDER BY l.member_account_id, b.id, l.property
  ON CONFLICT (member_account_id, org_badge_id) DO NOTHING
  RETURNING id, member_account_id
)
INSERT INTO badge_events (account_id, affiliation_id, action, actor, source)
SELECT member_account_id, id, 'affiliation_created', :actor, 'wikidata' FROM ins
RETURNING id
"""
)


# --- Wikidata ---------------------------------------------------------------------------


def batch_pairs(accounts: Sequence[WikidataAccount]) -> tuple[list[list[str]], int]:
    """Unambiguous (qid, x_id, handle) triples: valid X id, one QID per X id.

    Returns ``(triples, skipped)``. An X id claimed by several items is skipped (safety).
    """
    by_xid: dict[str, set[str]] = {}
    handles: dict[str, str | None] = {}
    skipped = 0
    for acc in accounts:
        try:
            validate_external_id(Platform.X, acc.x_id)
        except InvalidInput:
            skipped += 1
            continue
        by_xid.setdefault(acc.x_id, set()).add(acc.qid)
        handles.setdefault(acc.x_id, acc.handle)
    out: list[list[str]] = []
    for xid, qids in by_xid.items():
        if len(qids) != 1:
            skipped += 1
            continue
        out.append([next(iter(qids)), xid, handles.get(xid) or ""])
    out.sort()
    return out, skipped


async def apply_wikidata_batch(
    session: AsyncSession,
    actor: Actor,
    triples: Sequence[Sequence[str]],
    items: Mapping[str, WikidataItem],
) -> dict[str, int]:
    """Apply one batch of the import: badges, affiliation links, pending affiliations."""
    actor.require(Permission.BADGES_MANAGE)
    now = utcnow()
    stats = {"granted": 0, "updated": 0, "unchanged": 0, "unclassified": 0, "proposed": 0}
    members: list[int] = []
    org_qids: list[str] = []
    for qid, xid, handle in triples:
        item = items.get(qid)
        if item is None:
            stats["unclassified"] += 1
            continue
        category = classify_wikidata(item.types, item.occupations)
        account = await ensure_account(
            session, AccountRef(Platform.X, xid), handle=(handle or None)
        )
        if category is None:
            stats["unclassified"] += 1
        else:
            logo = (
                commons_file_url(item.logo)
                if item.logo and category in AFFILIABLE_CATEGORIES
                else None
            )
            action = await apply_automatic(
                session,
                actor,
                account,
                AutoGrant(
                    category=category,
                    source=BadgeSource.WIKIDATA,
                    label=item.label,
                    logo_url=logo,
                    wikidata_qid=qid,
                ),
            )
            key = action.value if action is not None else "unchanged"
            stats[key if key in stats else "updated"] += 1
            if category in AFFILIABLE_CATEGORIES:
                org_qids.append(qid)
        if item.links:
            members.append(account.id)
            stmt = insert(BadgeWikidataLink).values(
                [
                    {
                        "member_account_id": account.id,
                        "org_qid": org,
                        "property": prop,
                        "seen_at": now,
                    }
                    for prop, org in item.links
                ]
            )
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["member_account_id", "org_qid", "property"],
                    set_={"seen_at": now},
                )
            )
        await session.execute(
            delete(BadgeWikidataLink).where(
                BadgeWikidataLink.member_account_id == account.id,
                BadgeWikidataLink.seen_at < now,
            )
        )
    if members or org_qids:
        result = await session.execute(
            _PROPOSE_SQL,
            {
                "actor": actor.label,
                "affiliable": [c.value for c in AFFILIABLE_CATEGORIES],
                "members": members,
                "org_qids": org_qids,
            },
        )
        stats["proposed"] = len(result.all())
    if stats["proposed"]:
        await publish(
            session,
            "badge.affiliation_changed",
            {"action": "wikidata_proposed", "count": stats["proposed"]},
            aggregate_type="badge",
            actor=actor.label,
        )
    return stats


async def revoke_stale_wikidata(
    session: AsyncSession,
    actor: Actor,
    *,
    stale_days: int,
    fresh_window: timedelta,
    now: datetime | None = None,
    limit: int = 200,
) -> int:
    """Revoke Wikidata badges no longer seen for ``stale_days``.

    Guard: does nothing unless some Wikidata badge was seen within ``fresh_window``
    (the import is running): an import outage never revokes anything.
    """
    actor.require(Permission.BADGES_MANAGE)
    now = now or utcnow()
    fresh = await session.scalar(
        select(func.max(Badge.wikidata_seen_at)).where(Badge.source == BadgeSource.WIKIDATA.value)
    )
    if fresh is None or fresh < now - fresh_window:
        return 0
    rows = (
        await session.scalars(
            select(Badge)
            .where(
                Badge.status == BadgeStatus.ACTIVE.value,
                Badge.source == BadgeSource.WIKIDATA.value,
                or_(
                    Badge.wikidata_seen_at.is_(None),
                    Badge.wikidata_seen_at < now - timedelta(days=stale_days),
                ),
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).all()
    count = 0
    for badge in rows:
        if await revoke_automatic(session, actor, badge, reason="wikidata_no_longer_listed"):
            count += 1
    return count


async def scopes_due(
    session: AsyncSession, scopes: Sequence[str], *, interval: timedelta, now: datetime
) -> list[str]:
    """Scopes whose last import started more than ``interval`` ago; marks them started."""
    due: list[str] = []
    for scope in scopes:
        run = await session.get(BadgeImportRun, scope, with_for_update=True)
        if run is not None and run.started_at is not None and run.started_at > now - interval:
            continue
        if run is None:
            session.add(BadgeImportRun(scope=scope, started_at=now))
        else:
            run.started_at = now
        due.append(scope)
    await session.flush()
    return due


async def record_listing(
    session: AsyncSession, scope: str, *, accounts: int, batches: int, now: datetime
) -> None:
    stmt = insert(BadgeImportRun).values(
        scope=scope, started_at=now, listed_at=now, accounts=accounts, batches=batches
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["scope"],
            set_={"listed_at": now, "accounts": accounts, "batches": batches},
        )
    )


async def request_wikidata_import(
    session: AsyncSession, actor: Actor, scopes: Sequence[str]
) -> list[str]:
    """Team: start an import of ``scopes`` now (one listing job per scope)."""
    actor.require(Permission.BADGES_MANAGE)
    from tower.core.domain import error_codes as ec
    from tower.core.domain.badges import is_qid
    from tower.core.jobs.queue import enqueue

    clean = list(dict.fromkeys(s.strip() for s in scopes if s.strip()))
    bad = [s for s in clean if s != "intl" and not is_qid(s)]
    if not clean or bad:
        raise InvalidInput("Invalid import scope", code=ec.BDG_BAD_TEXT, scopes=bad[:5])
    now = utcnow()
    for scope in clean:
        await record_run_start(session, scope, now)
        await enqueue(session, QUEUE, SCOPE_JOB, {"scope": scope}, dedupe_key=f"badges:wd:{scope}")
    await publish(
        session,
        "badge.import_requested",
        {"scopes": clean},
        aggregate_type="badge",
        actor=actor.label,
    )
    return clean


async def record_run_start(session: AsyncSession, scope: str, now: datetime) -> None:
    stmt = insert(BadgeImportRun).values(scope=scope, started_at=now)
    await session.execute(
        stmt.on_conflict_do_update(index_elements=["scope"], set_={"started_at": now})
    )


async def import_runs(session: AsyncSession, actor: Actor) -> list[dict[str, Any]]:
    actor.require(Permission.BADGES_READ)
    rows = (await session.scalars(select(BadgeImportRun).order_by(BadgeImportRun.scope))).all()
    return [
        {
            "scope": r.scope,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "listed_at": r.listed_at.isoformat() if r.listed_at else None,
            "accounts": r.accounts,
            "batches": r.batches,
        }
        for r in rows
    ]


# --- X government verification ---------------------------------------------------------


async def hint_x_accounts(
    session: AsyncSession, actor: Actor, ids: Sequence[str], *, recheck_days: int
) -> int:
    """Extension hint: these X accounts show a government check mark.

    Hints are never trusted: the ``badges.x_verify`` job checks them with the X API (at
    most once per ``recheck_days`` per account, within a daily cap). Accounts that already
    hold an active or blocked badge are ignored. Returns the number of checks queued.
    """
    actor.require(Permission.BADGES_SUBMIT)
    _, clean = parse_lookup_ids(Platform.X.value, ids)
    now = utcnow()
    cutoff = now - timedelta(days=recheck_days)
    queued = 0
    for xid in clean:
        account = await ensure_account(session, AccountRef(Platform.X, xid))
        status = await session.scalar(select(Badge.status).where(Badge.account_id == account.id))
        if status in (BadgeStatus.ACTIVE.value, BadgeStatus.BLOCKED.value):
            continue
        stmt = insert(BadgeXCheck).values(account_id=account.id, hinted_at=now)
        result = await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["account_id"],
                set_={
                    "hinted_at": now,
                    "checked_at": func.nullif(
                        BadgeXCheck.checked_at,
                        func.least(BadgeXCheck.checked_at, cutoff),
                    ),
                },
            ).returning(BadgeXCheck.checked_at)
        )
        if result.scalar_one() is None:
            queued += 1
    if queued:
        from tower.core.jobs.queue import enqueue

        # Short delay: hints arriving together are checked in one X call.
        await enqueue(session, QUEUE, X_JOB, {}, delay=60, dedupe_key="badges:x")
    return queued


async def due_x_checks(
    session: AsyncSession, *, limit: int, recheck_days: int, now: datetime | None = None
) -> list[str]:
    """X ids to check: hinted and unchecked, then X government badges due a recheck."""
    now = now or utcnow()
    if limit <= 0:
        return []
    hinted = (
        await session.scalars(
            select(PlatformAccount.external_id)
            .join(BadgeXCheck, BadgeXCheck.account_id == PlatformAccount.id)
            .where(BadgeXCheck.checked_at.is_(None), BadgeXCheck.hinted_at.is_not(None))
            .order_by(BadgeXCheck.hinted_at)
            .limit(limit)
        )
    ).all()
    out = [x for x in hinted if x]
    if len(out) < limit:
        cutoff = now - timedelta(days=recheck_days)
        recheck = (
            await session.scalars(
                select(PlatformAccount.external_id)
                .join(Badge, Badge.account_id == PlatformAccount.id)
                .outerjoin(BadgeXCheck, BadgeXCheck.account_id == PlatformAccount.id)
                .where(
                    Badge.status == BadgeStatus.ACTIVE.value,
                    Badge.source == BadgeSource.X_GOVERNMENT.value,
                    or_(BadgeXCheck.checked_at.is_(None), BadgeXCheck.checked_at < cutoff),
                )
                .order_by(BadgeXCheck.checked_at.nulls_first())
                .limit(limit - len(out))
            )
        ).all()
        out.extend(x for x in recheck if x and x not in out)
    return out


async def x_checks_since(session: AsyncSession, since: datetime) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(BadgeXCheck).where(BadgeXCheck.checked_at >= since)
        )
        or 0
    )


async def apply_x_results(
    session: AsyncSession,
    actor: Actor,
    checked_ids: Sequence[str],
    accounts: Sequence[PublicAccount],
    *,
    government_type: str,
) -> dict[str, int]:
    """Record the checks; grant X government badges, revoke those X no longer confirms
    (absent from the answer = suspended, deleted or no longer verified)."""
    actor.require(Permission.BADGES_MANAGE)
    now = utcnow()
    found = {a.external_id: a for a in accounts}
    stats = {"granted": 0, "updated": 0, "revoked": 0, "checked": 0}
    for xid in checked_ids:
        account = await ensure_account(session, AccountRef(Platform.X, xid))
        info = found.get(xid)
        verified_type = info.verified_type if info is not None else None
        stmt = insert(BadgeXCheck).values(
            account_id=account.id, checked_at=now, verified_type=verified_type
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["account_id"],
                set_={"checked_at": now, "verified_type": verified_type},
            )
        )
        stats["checked"] += 1
        if verified_type == government_type:
            action = await apply_automatic(
                session,
                actor,
                account,
                AutoGrant(
                    category=BadgeCategory.INSTITUTIONAL,
                    source=BadgeSource.X_GOVERNMENT,
                    label=info.display_name if info is not None else None,
                ),
            )
            if action is not None:
                stats["granted" if action.value == "granted" else "updated"] += 1
            continue
        badge = await session.scalar(select(Badge).where(Badge.account_id == account.id))
        if (
            badge is not None
            and badge.status == BadgeStatus.ACTIVE.value
            and badge.source == BadgeSource.X_GOVERNMENT.value
            and await revoke_automatic(session, actor, badge, reason="x_verification_lost")
        ):
            stats["revoked"] += 1
    return stats
