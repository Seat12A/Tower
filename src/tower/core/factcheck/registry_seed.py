"""Initial ``source_registry`` rows (minimal reliability registry).

Tiers rest on the **editorial process** only (official producer of the data or text; press
agency or fact-checker with a public correction policy; the rest tertiary), never on
political orientation. A domain is a registrable domain or a host suffix: ``gouv.fr``
covers every ``*.gouv.fr`` host, the most specific entry wins. Seed rows never overwrite
an existing row (the registry is editable afterwards).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from tower.core.models.factcheck import SourceRegistryEntry

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from tower.core.config import Settings

__all__ = ["SEED", "run_seed_registry", "seed_registry"]

#: (domain, publisher, tier, kind, country)
SEED: tuple[tuple[str, str, str, str, str | None], ...] = (
    # France: institutions (host suffix gouv.fr covers ministries and agencies)
    ("gouv.fr", "French government and administrations", "primary", "institution", "FR"),
    ("insee.fr", "INSEE", "primary", "statistics", "FR"),
    ("legifrance.gouv.fr", "Légifrance (DILA)", "primary", "institution", "FR"),
    ("data.gouv.fr", "data.gouv.fr", "primary", "institution", "FR"),
    ("vie-publique.fr", "Vie publique (DILA)", "primary", "institution", "FR"),
    ("assemblee-nationale.fr", "Assemblée nationale", "primary", "parliament", "FR"),
    ("senat.fr", "Sénat", "primary", "parliament", "FR"),
    ("conseil-constitutionnel.fr", "Conseil constitutionnel", "primary", "court", "FR"),
    ("conseil-etat.fr", "Conseil d'État", "primary", "court", "FR"),
    ("courdecassation.fr", "Cour de cassation", "primary", "court", "FR"),
    ("ccomptes.fr", "Cour des comptes", "primary", "institution", "FR"),
    ("banque-france.fr", "Banque de France", "primary", "institution", "FR"),
    ("santepubliquefrance.fr", "Santé publique France", "primary", "institution", "FR"),
    ("ined.fr", "INED", "primary", "statistics", "FR"),
    ("elysee.fr", "Présidence de la République", "primary", "institution", "FR"),
    # European Union (suffix covers eur-lex.europa.eu, ec.europa.eu/eurostat…)
    ("europa.eu", "European Union institutions", "primary", "institution", "EU"),
    ("europarl.europa.eu", "European Parliament", "primary", "parliament", "EU"),
    ("curia.europa.eu", "Court of Justice of the European Union", "primary", "court", "EU"),
    # International organisations
    ("un.org", "United Nations", "primary", "institution", "INT"),
    ("who.int", "World Health Organization", "primary", "institution", "INT"),
    ("worldbank.org", "World Bank", "primary", "institution", "INT"),
    ("imf.org", "International Monetary Fund", "primary", "institution", "INT"),
    ("oecd.org", "OECD", "primary", "institution", "INT"),
    ("ipcc.ch", "IPCC", "primary", "academic", "INT"),
    ("icj-cij.org", "International Court of Justice", "primary", "court", "INT"),
    ("icc-cpi.int", "International Criminal Court", "primary", "court", "INT"),
    ("unhcr.org", "UNHCR", "primary", "institution", "INT"),
    ("ilo.org", "International Labour Organization", "primary", "institution", "INT"),
    # National statistics offices and legal portals
    ("ons.gov.uk", "Office for National Statistics", "primary", "statistics", "GB"),
    ("destatis.de", "Destatis", "primary", "statistics", "DE"),
    ("census.gov", "US Census Bureau", "primary", "statistics", "US"),
    ("bls.gov", "US Bureau of Labor Statistics", "primary", "statistics", "US"),
    ("istat.it", "ISTAT", "primary", "statistics", "IT"),
    ("ine.es", "INE (Spain)", "primary", "statistics", "ES"),
    ("statbel.fgov.be", "Statbel", "primary", "statistics", "BE"),
    ("bfs.admin.ch", "Federal Statistical Office (Switzerland)", "primary", "statistics", "CH"),
    ("statcan.gc.ca", "Statistics Canada", "primary", "statistics", "CA"),
    ("fedlex.admin.ch", "Fedlex (Swiss federal law)", "primary", "institution", "CH"),
    ("admin.ch", "Swiss federal administration", "primary", "institution", "CH"),
    ("gov.uk", "UK government", "primary", "institution", "GB"),
    ("legislation.gov.uk", "UK legislation", "primary", "institution", "GB"),
    ("bundesregierung.de", "German federal government", "primary", "institution", "DE"),
    ("gesetze-im-internet.de", "German federal law", "primary", "institution", "DE"),
    ("canada.ca", "Government of Canada", "primary", "institution", "CA"),
    ("justice.gc.ca", "Justice Canada", "primary", "institution", "CA"),
    ("fgov.be", "Belgian federal government", "primary", "institution", "BE"),
    # News agencies (secondary: editorial process, public corrections)
    ("afp.com", "AFP", "secondary", "agency", "FR"),
    ("reuters.com", "Reuters", "secondary", "agency", "INT"),
    ("apnews.com", "Associated Press", "secondary", "agency", "US"),
    ("dpa.com", "dpa", "secondary", "agency", "DE"),
    ("ansa.it", "ANSA", "secondary", "agency", "IT"),
    ("belga.be", "Belga", "secondary", "agency", "BE"),
    # Fact-checkers (secondary)
    ("factuel.afp.com", "AFP Factuel", "secondary", "fact_checker", "FR"),
    # Encyclopedic and collaborative sources (tertiary)
    ("wikipedia.org", "Wikipedia", "tertiary", "encyclopedia", "INT"),
    ("wikidata.org", "Wikidata", "tertiary", "encyclopedia", "INT"),
)


async def seed_registry(session: AsyncSession) -> tuple[int, int]:
    """Insert the missing seed domains (origin ``seed``); never touch existing rows.

    Returns ``(inserted, kept)``.
    """
    existing = set((await session.execute(select(SourceRegistryEntry.domain))).scalars())
    rows = [
        {
            "domain": domain,
            "publisher": publisher,
            "tier": tier,
            "kind": kind,
            "country": country,
            "origin": "seed",
        }
        for domain, publisher, tier, kind, country in SEED
        if domain not in existing
    ]
    if rows:
        await session.execute(
            pg_insert(SourceRegistryEntry)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["domain"])
        )
    return len(rows), len(SEED) - len(rows)


async def run_seed_registry(settings: Settings) -> None:
    """Seed the registry in one transaction and print the outcome (CLI helper)."""
    from tower.core.db.database import Database

    db = Database(settings)
    try:
        async with db.transaction() as session:
            inserted, kept = await seed_registry(session)
    finally:
        await db.dispose()
    print(f"Registry: {inserted} inserted, {kept} kept.")
