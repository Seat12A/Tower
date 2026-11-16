"""Standing of retrieved sources at collection time (docs/factcheck/sources.md).

Applies the two steps that need the database or the claim on top of the Atlas rating
(registry, suffix rules, ``unrated``): the ``domain_identities`` table (one ``SELECT`` per
collection round, never loaded in memory) and the official site of the claim's subject
(``first_party``, per passage).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.adapters.knowledge.atlas import AtlasDocument
from tower.core.domain.factcheck.standing import (
    TierBasis,
    host_matches,
    host_suffixes,
    identity_standing,
)
from tower.core.models.atlas import DomainIdentity

__all__ = ["apply_identities", "first_party_urls", "identity_lookup", "url_host"]

_OVERRIDABLE = frozenset({TierBasis.SUFFIX.value, TierBasis.DEFAULT.value})
"""Only a rating from the suffix rules or the default is refined by an identity: the
registry and the integrated adapters keep theirs."""


def url_host(url: str) -> str:
    try:
        host = urlsplit(url if "//" in url else f"//{url}").hostname or ""
    except ValueError:
        host = ""
    return host.lower().rstrip(".").removeprefix("www.")


async def identity_lookup(session: AsyncSession, hosts: Iterable[str]) -> dict[str, str]:
    """Host → identity kind of its most specific known suffix (absent when none)."""
    suffixes = {h: host_suffixes(h) for h in {x for x in hosts if x}}
    wanted = sorted({s for items in suffixes.values() for s in items})
    if not wanted:
        return {}
    rows = (
        await session.execute(
            select(DomainIdentity.domain, DomainIdentity.kind).where(
                DomainIdentity.domain.in_(wanted)
            )
        )
    ).all()
    kinds = {r.domain: r.kind for r in rows}
    out: dict[str, str] = {}
    for host, items in suffixes.items():
        for suffix in items:  # most specific first
            if suffix in kinds:
                out[host] = kinds[suffix]
                break
    return out


async def apply_identities(
    session: AsyncSession, documents: Sequence[AtlasDocument]
) -> list[AtlasDocument]:
    """Documents rated by the suffix rules or by default, refined by the identity table."""
    candidates = [d for d in documents if (d.tier_basis or "") in _OVERRIDABLE]
    if not candidates:
        return list(documents)
    kinds = await identity_lookup(session, (url_host(d.canonical_url) for d in candidates))
    chosen = {id(d) for d in candidates}
    out: list[AtlasDocument] = []
    for doc in documents:
        kind = kinds.get(url_host(doc.canonical_url)) if id(doc) in chosen else None
        rated = identity_standing(kind) if kind else None
        if rated is None:
            out.append(doc)
            continue
        tier, standing = rated
        out.append(
            replace(doc, tier=tier, standing=standing.value, tier_basis=TierBasis.IDENTITY.value)
        )
    return out


def first_party_urls(urls: Iterable[str], subject_hosts: Sequence[str]) -> set[str]:
    """URLs published on an official site of the claim's subject (or a subdomain)."""
    if not subject_hosts:
        return set()
    return {u for u in urls if host_matches(url_host(u), subject_hosts)}
