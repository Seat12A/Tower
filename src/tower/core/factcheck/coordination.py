"""Coordination signals of a claim group (read-only SQL, informative only).

The signals help reviewers read a draft ("many near-identical posts in a few minutes").
They never enter a verdict, a priority or a grouping decision, and they say nothing about
a political side (docs/factcheck/vagues.md). Queries are bounded: only the most recent
contents of the group are examined.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "BURST_ACCOUNTS",
    "BURST_WINDOW_MINUTES",
    "INSTALLATION_BURST_OBSERVATIONS",
    "NEW_ACCOUNT_DAYS",
    "NEW_ACCOUNT_MIN_AUTHORS",
    "NEW_ACCOUNT_SHARE",
    "coordination_signals",
]

BURST_ACCOUNTS = 10
BURST_WINDOW_MINUTES = 10
NEW_ACCOUNT_DAYS = 7
NEW_ACCOUNT_SHARE = 0.5
NEW_ACCOUNT_MIN_AUTHORS = 10
INSTALLATION_BURST_OBSERVATIONS = 20
MEDIA_MIN_GROUPS = 2

_MAX_CONTENTS = 1000
"""Most recent contents of the group examined (bounds every query)."""
_MAX_POOL = 1500
_MAX_OBSERVATIONS = 5000
_OBSERVATION_DAYS = 7

_BASE = f"""
SELECT DISTINCT ci.id, ci.author_account_id, ci.content_hash, ci.copy_of_id,
       COALESCE(ci.created_at_platform, ci.first_seen_at) AS ts
FROM claim_group_members m
JOIN content_claims cc ON cc.claim_id = m.claim_id
JOIN content_items ci ON ci.id = cc.content_id
WHERE m.group_id = :gid AND m.removed_at IS NULL
ORDER BY ts DESC
LIMIT {_MAX_CONTENTS}
"""  # noqa: S608 (constant fragment)


def _signal(kind: str, count: int, window_minutes: int, **detail: Any) -> dict[str, Any]:
    return {"kind": kind, "count": count, "window_minutes": window_minutes, "detail": detail}


async def _near_identical_burst(session: AsyncSession, gid: int) -> dict[str, Any] | None:
    """Most distinct accounts posting copies of the group's contents in one 10 minutes
    window; copies = same ``copy_of_id`` root, or same ``content_hash``."""
    best = 0
    basis = ""
    for key, label in (("root", "copy_of"), ("h", "content_hash")):
        count = int(
            await session.scalar(
                text(
                    f"WITH base AS ({_BASE}), "  # noqa: S608 (constant fragments)
                    "roots AS (SELECT DISTINCT COALESCE(copy_of_id, id) AS root FROM base), "
                    "hashes AS (SELECT DISTINCT content_hash FROM base), "
                    "pool AS (SELECT ci.author_account_id AS author, "
                    "COALESCE(ci.created_at_platform, ci.first_seen_at) AS ts, "
                    "COALESCE(ci.copy_of_id, ci.id) AS root, ci.content_hash AS h "
                    "FROM content_items ci WHERE ci.id IN (SELECT root FROM roots) "
                    "OR ci.copy_of_id IN (SELECT root FROM roots) "
                    "OR ci.content_hash IN (SELECT content_hash FROM hashes) "
                    f"LIMIT {_MAX_POOL}) "
                    "SELECT COALESCE(max((SELECT count(DISTINCT b.author) FROM pool b "
                    f"WHERE b.{key} = a.{key} AND b.ts >= a.ts "
                    "AND b.ts < a.ts + make_interval(mins => :w))), 0) FROM pool a"
                ),
                {"gid": gid, "w": BURST_WINDOW_MINUTES},
            )
            or 0
        )
        if count > best:
            best, basis = count, label
    if best < BURST_ACCOUNTS:
        return None
    return _signal("near_identical_burst", best, BURST_WINDOW_MINUTES, basis=basis, accounts=best)


async def _new_accounts(session: AsyncSession, gid: int, now: datetime) -> dict[str, Any] | None:
    """Share of authors first seen by us within the last 7 days. ``created_at`` is the
    first sighting by Tower, not the real age of the account."""
    row = (
        await session.execute(
            text(
                f"WITH base AS ({_BASE}) "  # noqa: S608 (constant fragment)
                "SELECT count(*), count(*) FILTER (WHERE pa.created_at >= :since) "
                "FROM (SELECT DISTINCT author_account_id AS a FROM base "
                "WHERE author_account_id IS NOT NULL) authors "
                "JOIN platform_accounts pa ON pa.id = authors.a"
            ),
            {"gid": gid, "since": now - timedelta(days=NEW_ACCOUNT_DAYS)},
        )
    ).one()
    authors, fresh = int(row[0]), int(row[1])
    if authors < NEW_ACCOUNT_MIN_AUTHORS or fresh / authors < NEW_ACCOUNT_SHARE:
        return None
    return _signal(
        "new_accounts",
        fresh,
        NEW_ACCOUNT_DAYS * 24 * 60,
        authors=authors,
        share=round(fresh / authors, 3),
        note="first seen by Tower within the window; not the real age of the accounts",
    )


async def _installation_burst(
    session: AsyncSession, gid: int, now: datetime
) -> dict[str, Any] | None:
    """Most observations of the group's contents by one installation in 10 minutes."""
    count = int(
        await session.scalar(
            text(
                f"WITH base AS ({_BASE}), "  # noqa: S608 (constant fragment)
                "obs AS (SELECT o.installation_pseudonym AS p, "
                "extract(epoch FROM o.observed_at) AS t FROM observations o "
                "WHERE o.content_id IN (SELECT id FROM base) "
                "AND o.installation_pseudonym IS NOT NULL AND o.observed_at >= :since "
                f"ORDER BY o.observed_at DESC LIMIT {_MAX_OBSERVATIONS}) "
                "SELECT COALESCE(max(n), 0) FROM (SELECT count(*) OVER (PARTITION BY p "
                "ORDER BY t RANGE BETWEEN CURRENT ROW AND :span FOLLOWING) AS n FROM obs) w"
            ),
            {
                "gid": gid,
                "since": now - timedelta(days=_OBSERVATION_DAYS),
                "span": float(BURST_WINDOW_MINUTES * 60 - 1),
            },
        )
        or 0
    )
    if count < INSTALLATION_BURST_OBSERVATIONS:
        return None
    return _signal("installation_burst", count, BURST_WINDOW_MINUTES, observations=count)


async def _media_reuse(session: AsyncSession, gid: int) -> dict[str, Any] | None:
    """One media asset carried by contents of at least two different groups (captions
    may contradict each other: the team checks)."""
    rows = (
        await session.execute(
            text(
                f"WITH base AS ({_BASE}), "  # noqa: S608 (constant fragment)
                "media AS (SELECT DISTINCT cm.media_id FROM content_media cm "
                "WHERE cm.content_id IN (SELECT id FROM base) LIMIT 200) "
                "SELECT cm.media_id, array_agg(DISTINCT cg.public_id::text) "
                "FROM media JOIN content_media cm ON cm.media_id = media.media_id "
                "JOIN content_claims cc ON cc.content_id = cm.content_id "
                "JOIN claim_group_members m ON m.claim_id = cc.claim_id "
                "AND m.removed_at IS NULL "
                "JOIN claim_groups cg ON cg.id = m.group_id "
                "GROUP BY cm.media_id HAVING count(DISTINCT m.group_id) >= :min"
            ),
            {"gid": gid, "min": MEDIA_MIN_GROUPS},
        )
    ).all()
    if not rows:
        return None
    own = await session.scalar(
        text("SELECT public_id::text FROM claim_groups WHERE id = :g"), {"g": gid}
    )
    others = sorted({g for _, groups in rows for g in groups if g != own})
    widest = max(len(groups) for _, groups in rows)
    return _signal(
        "media_reuse",
        widest,
        0,
        media_assets=len(rows),
        other_groups=others[:10],
    )


async def coordination_signals(
    session: AsyncSession, *, group_id: int, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Signals observed on the group's contents: ``{kind, count, window_minutes, detail}``.

    Informative only: never an input of a verdict, a priority or a grouping."""
    now = now or datetime.now(UTC)
    found = (
        await _near_identical_burst(session, group_id),
        await _new_accounts(session, group_id, now),
        await _installation_burst(session, group_id, now),
        await _media_reuse(session, group_id),
    )
    return [s for s in found if s is not None]
