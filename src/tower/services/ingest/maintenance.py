"""Periodic maintenance: observation partitions, installation secrets."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.config import Settings
from tower.core.usecases.installations import daily_secret

PARTITION_RE = re.compile(r"^observations_p(\d{8})$")
DAILY_RETENTION_DAYS = 30

_EXISTS = text("SELECT to_regclass(:name) IS NOT NULL")
_LIST = text(
    """
    SELECT c.relname FROM pg_inherits i
    JOIN pg_class c ON c.oid = i.inhrelid
    WHERE i.inhparent = 'observations'::regclass
    """
)


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _partition_name(week: date) -> str:
    return f"observations_p{week:%Y%m%d}"


async def ensure_partitions(session: AsyncSession, now: datetime, *, weeks_ahead: int = 2) -> int:
    """Create the partitions of the current week and the next ``weeks_ahead`` ones."""
    start = _monday(now.astimezone(UTC).date())
    created = 0
    for i in range(weeks_ahead + 1):
        week = start + timedelta(days=7 * i)
        name = _partition_name(week)  # built from a date only: safe to inline
        if (await session.execute(_EXISTS, {"name": f"public.{name}"})).scalar_one():
            continue
        await session.execute(
            text(
                f'CREATE TABLE IF NOT EXISTS "{name}" PARTITION OF observations '
                f"FOR VALUES FROM ('{week.isoformat()} 00:00+00') "
                f"TO ('{(week + timedelta(days=7)).isoformat()} 00:00+00')"
            )
        )
        created += 1
    return created


async def purge_partitions(
    session: AsyncSession, now: datetime, *, retention_days: int = 30
) -> int:
    """Aggregate then drop partitions whose week ended at least ``retention_days`` ago."""
    cutoff = now.astimezone(UTC) - timedelta(days=retention_days)
    aggregated = 0
    names = [r[0] for r in await session.execute(_LIST)]
    for name in sorted(names):
        match = PARTITION_RE.match(name)
        if match is None:
            continue
        week_end = datetime.strptime(match.group(1), "%Y%m%d").replace(tzinfo=UTC) + timedelta(
            days=7
        )
        if week_end > cutoff:
            continue
        # ``name`` matched ^observations_p\d{8}$: safe to inline as an identifier.
        result = await session.execute(
            text(
                "INSERT INTO observation_counters (content_id, hour, source, count) "  # noqa: S608
                "SELECT content_id, date_trunc('hour', observed_at), source, count(*) "
                f'FROM "{name}" GROUP BY 1, 2, 3 '
                "ON CONFLICT (content_id, hour, source) DO UPDATE "
                "SET count = observation_counters.count + EXCLUDED.count"
            )
        )
        aggregated += int(result.rowcount)  # type: ignore[attr-defined]
        await session.execute(text(f'DROP TABLE "{name}"'))
    return aggregated


async def maintain_partitions(session: AsyncSession, settings: Settings) -> int:
    now = datetime.now(UTC)
    created = await ensure_partitions(session, now, weeks_ahead=2)
    return created + await purge_partitions(session, now, retention_days=30)


async def rotate(session: AsyncSession, today: date) -> int:
    """Create today's and tomorrow's secrets; delete past secrets and old daily rows."""
    await daily_secret(session, today)
    await daily_secret(session, today + timedelta(days=1))
    secrets = await session.execute(
        text("DELETE FROM installation_secrets WHERE day < :today"), {"today": today}
    )
    daily = await session.execute(
        text("DELETE FROM installation_daily WHERE day < :limit"),
        {"limit": today - timedelta(days=DAILY_RETENTION_DAYS)},
    )
    return int(secrets.rowcount) + int(daily.rowcount)  # type: ignore[attr-defined]


async def rotate_secrets(session: AsyncSession, settings: Settings) -> int:
    return await rotate(session, datetime.now(UTC).date())
