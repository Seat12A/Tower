"""Initial country registries: ``jurisdiction_sources``, ``legal_portals``, ``election_calendar``.

Idempotent: a missing row is inserted; a row never edited by a human (``updated_by IS NULL``)
is refreshed when the seed changed (version + 1); a row edited through the API is never
touched. Source tiers rest on the editorial process (official producer of the data or
text), never on political orientation.

``ELECTIONS`` is empty on purpose: no election date could be confirmed from an official
source when this seed was written (2026-11-14); editors add them through the API.
"""

from __future__ import annotations

from datetime import date
from typing import Any, NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.models.atlas import ElectionEntry, JurisdictionSource, LegalPortal

__all__ = ["ELECTIONS", "LEGAL_PORTALS", "SOURCES", "seed_country_registries"]


class SourceSeed(NamedTuple):
    country: str
    source_type: str
    key: str
    adapter: str | None
    domain: str | None
    name: str


class PortalSeed(NamedTuple):
    country: str
    name: str
    url: str
    access: str
    eli: bool
    adapter: str | None
    notes: str


class ElectionSeed(NamedTuple):
    country: str
    kind: str
    name: str
    first_round: date
    second_round: date | None
    source_url: str
    notes: str


_S = SourceSeed
SOURCES: tuple[SourceSeed, ...] = (
    _S("FR", "statistics", "insee", "insee", "insee.fr", "INSEE"),
    _S("FR", "law", "legi_mirror", "legi_mirror", None, "LEGI mirror (Légifrance bulk data)"),
    _S("FR", "law", "legifrance", "legifrance", "legifrance.gouv.fr", "Légifrance"),
    _S("FR", "electoral", "interieur", None, "interieur.gouv.fr", "Ministère de l'Intérieur"),
    _S(
        "FR",
        "electoral",
        "conseil_constitutionnel",
        None,
        "conseil-constitutionnel.fr",
        "Conseil constitutionnel",
    ),
    _S("FR", "central_bank", "banque_de_france", None, "banque-france.fr", "Banque de France"),
    _S("FR", "health", "spf", None, "santepubliquefrance.fr", "Santé publique France"),
    _S("FR", "justice", "justice", None, "justice.gouv.fr", "Ministère de la Justice"),
    _S("EU", "statistics", "eurostat", "eurostat", None, "Eurostat"),
    _S("EU", "law", "eurlex", "eurlex", None, "EUR-Lex"),
    _S("CH", "law", "fedlex", "fedlex", None, "Fedlex"),
    _S("CH", "statistics", "bfs", None, "bfs.admin.ch", "Federal Statistical Office (BFS)"),
    _S("CA", "law", "canada_laws", "canada_laws", None, "Justice Laws Website"),
    _S("CA", "statistics", "statcan", None, "statcan.gc.ca", "Statistics Canada"),
    _S("BE", "statistics", "statbel", None, "statbel.fgov.be", "Statbel"),
    _S("BE", "law", "ejustice", None, "ejustice.just.fgov.be", "Moniteur belge (eJustice)"),
    _S("INT", "statistics", "worldbank", "worldbank", None, "World Bank"),
    _S("INT", "statistics", "imf", "imf", None, "International Monetary Fund"),
    _S("INT", "statistics", "oecd", "oecd", None, "OECD"),
)

LEGAL_PORTALS: tuple[PortalSeed, ...] = (
    PortalSeed(
        "FR",
        "Légifrance",
        "https://www.legifrance.gouv.fr/",
        "bulk",
        True,
        "legi_mirror",
        "LEGI bulk archives (DILA open data); ELI supported.",
    ),
    PortalSeed(
        "EU",
        "EUR-Lex",
        "https://eur-lex.europa.eu/",
        "api",
        True,
        "eurlex",
        "Cellar SPARQL / REST; ELI supported.",
    ),
    PortalSeed(
        "CH",
        "Fedlex",
        "https://www.fedlex.admin.ch/",
        "api",
        True,
        "fedlex",
        "SPARQL endpoint (JOLUX); ELI supported.",
    ),
    PortalSeed(
        "CA",
        "Justice Laws Website",
        "https://laws-lois.justice.gc.ca/",
        "bulk",
        False,
        "canada_laws",
        "XML bulk data of the consolidated acts and regulations.",
    ),
    PortalSeed(
        "BE",
        "Justel",
        "https://www.ejustice.just.fgov.be/cgi_loi/legislation.pl",
        "unverified",
        True,
        None,
        "Automated access not verified.",
    ),
    PortalSeed(
        "LU",
        "Legilux",
        "https://legilux.public.lu/",
        "unverified",
        True,
        None,
        "Automated access not verified.",
    ),
    PortalSeed(
        "CA-QC",
        "LégisQuébec",
        "https://www.legisquebec.gouv.qc.ca/",
        "unverified",
        False,
        None,
        "Automated access not verified.",
    ),
)

ELECTIONS: tuple[ElectionSeed, ...] = ()


async def _sync(session: AsyncSession, model: Any, where: list[Any], values: dict[str, Any]) -> int:
    """Insert or refresh one row; return 1 when something was written."""
    row = await session.scalar(select(model).where(*where).with_for_update())
    if row is None:
        session.add(model(**values, version=1))
        return 1
    if row.updated_by is not None:
        return 0
    changed = {k: v for k, v in values.items() if getattr(row, k) != v}
    if not changed:
        return 0
    for key, value in changed.items():
        setattr(row, key, value)
    row.version += 1
    return 1


async def seed_country_registries(session: AsyncSession) -> dict[str, int]:
    """Seed the three tables in the caller's transaction; counts the rows written."""
    counts = {"jurisdiction_sources": 0, "legal_portals": 0, "election_calendar": 0}
    for s in SOURCES:
        counts["jurisdiction_sources"] += await _sync(
            session,
            JurisdictionSource,
            [
                JurisdictionSource.country == s.country,
                JurisdictionSource.source_type == s.source_type,
                JurisdictionSource.key == s.key,
            ],
            {
                "country": s.country,
                "source_type": s.source_type,
                "key": s.key,
                "adapter": s.adapter,
                "domain": s.domain,
                "name": s.name,
                "tier": "primary",
                "notes": "Official producer of the data or text (seed).",
            },
        )
    for p in LEGAL_PORTALS:
        domain = p.url.split("//", 1)[1].split("/", 1)[0].removeprefix("www.")
        counts["legal_portals"] += await _sync(
            session,
            LegalPortal,
            [LegalPortal.country == p.country, LegalPortal.url == p.url],
            {
                "country": p.country,
                "name": p.name,
                "url": p.url,
                "domain": domain,
                "eli": p.eli,
                "access": p.access,
                "adapter": p.adapter,
                "notes": p.notes,
            },
        )
    for e in ELECTIONS:
        counts["election_calendar"] += await _sync(
            session,
            ElectionEntry,
            [
                ElectionEntry.country == e.country,
                ElectionEntry.kind == e.kind,
                ElectionEntry.first_round == e.first_round,
            ],
            {
                "country": e.country,
                "kind": e.kind,
                "name": e.name,
                "first_round": e.first_round,
                "second_round": e.second_round,
                "source_url": e.source_url,
                "notes": e.notes,
            },
        )
    await session.flush()
    return counts
