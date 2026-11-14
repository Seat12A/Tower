"""Publisher identities from ``source_registry`` for the stakeholder rule (E7, Q11).

One ``SELECT`` per investigation step that needs it; the lookup matches the host suffix
(most specific registry entry wins), like ``Atlas.tier_of``.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain.factcheck.atlas import PublisherIdentity
from tower.core.models.factcheck import SourceRegistryEntry

__all__ = ["PublisherLookup", "load_publishers"]

PublisherLookup = Callable[[str], PublisherIdentity | None]


def _host(value: str) -> str:
    raw = value.strip()
    try:
        host = urlsplit(raw if "//" in raw else f"//{raw}").hostname or ""
    except ValueError:
        host = ""
    return host.lower().rstrip(".").removeprefix("www.")


async def load_publishers(session: AsyncSession) -> PublisherLookup:
    rows = (
        await session.execute(
            select(
                SourceRegistryEntry.domain,
                SourceRegistryEntry.publisher,
                SourceRegistryEntry.aliases,
                SourceRegistryEntry.controlled_by,
                SourceRegistryEntry.controlled_by_aliases,
            ).where(SourceRegistryEntry.active.is_(True))
        )
    ).all()
    table = {
        r.domain.lower(): PublisherIdentity(
            names=(r.publisher, *(r.aliases or ())),
            controllers=tuple(n for n in (r.controlled_by, *(r.controlled_by_aliases or ())) if n),
        )
        for r in rows
    }

    def lookup(url_or_domain: str) -> PublisherIdentity | None:
        labels = _host(url_or_domain).split(".")
        for i in range(len(labels)):
            found = table.get(".".join(labels[i:]))
            if found is not None:
                return found
        return None

    return lookup
