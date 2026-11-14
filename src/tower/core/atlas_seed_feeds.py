"""Seed list of reference news feeds (RSS/Atom) for the Atlas news index.

Neutrality: the list covers public service, national and regional press of diverse
editorial sensibilities, and international outlets; no feed is handled differently by its
orientation (same polling, same retention, same clustering). Only official feeds that
answered with a parseable document when the list was built (2026-11-14) are kept. Outlets
whose feed answered 403/404/410 then (Le Point, Les Echos, La Croix, Marianne, Le Parisien,
La Voix du Nord, Le Telegramme, Le Soir, L'Opinion) are left out for now, and AFP has no free
feed: the editors can add them through the ``news_feeds`` table. The index only keeps
headline, lead, date and link (never the article).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.adapters.knowledge.atlas import registrable_domain
from tower.core.models.atlas import NewsFeed

__all__ = ["SEED_FEEDS", "SeedFeed", "seed_news_feeds"]


@dataclass(frozen=True, slots=True)
class SeedFeed:
    country: str
    lang: str
    publisher: str
    kind: str
    url: str
    reason: str
    """One line: why this feed is in the reference list."""


def _f(country: str, lang: str, publisher: str, kind: str, url: str, reason: str) -> SeedFeed:
    return SeedFeed(country, lang, publisher, kind, url, reason)


SEED_FEEDS: tuple[SeedFeed, ...] = (
    # France: public service
    _f(
        "FR",
        "fr",
        "franceinfo",
        "public_service",
        "https://www.francetvinfo.fr/titres.rss",
        "Public television and radio news",
    ),
    _f(
        "FR",
        "fr",
        "France Inter",
        "public_service",
        "https://www.radiofrance.fr/franceinter/rss",
        "Public radio, general news",
    ),
    _f(
        "FR",
        "fr",
        "RFI",
        "public_service",
        "https://www.rfi.fr/fr/rss",
        "Public international radio, French edition",
    ),
    _f(
        "FR",
        "fr",
        "France 24",
        "public_service",
        "https://www.france24.com/fr/rss",
        "Public international television, French edition",
    ),
    # France: national press
    _f(
        "FR",
        "fr",
        "Le Monde",
        "national",
        "https://www.lemonde.fr/rss/une.xml",
        "National daily of record",
    ),
    _f(
        "FR",
        "fr",
        "Le Figaro",
        "national",
        "https://www.lefigaro.fr/rss/figaro_actualites.xml",
        "National daily, centre-right",
    ),
    _f(
        "FR",
        "fr",
        "Libération",
        "national",
        "https://www.liberation.fr/arc/outboundfeeds/rss-all/collection/accueil-une/?outputType=xml",
        "National daily, centre-left",
    ),
    _f(
        "FR", "fr", "L'Humanité", "national", "https://www.humanite.fr/feed", "National daily, left"
    ),
    _f(
        "FR",
        "fr",
        "Valeurs actuelles",
        "national",
        "https://www.valeursactuelles.com/feed",
        "National weekly, right",
    ),
    _f(
        "FR",
        "fr",
        "20 Minutes",
        "national",
        "https://www.20minutes.fr/feeds/rss-une.xml",
        "Free national daily, general audience",
    ),
    _f(
        "FR",
        "fr",
        "L'Express",
        "national",
        "https://www.lexpress.fr/rss/alaune.xml",
        "National weekly, general news",
    ),
    _f(
        "FR",
        "fr",
        "Mediapart",
        "national",
        "https://www.mediapart.fr/articles/feed",
        "Independent investigative outlet",
    ),
    _f(
        "FR",
        "fr",
        "Le JDD",
        "national",
        "https://www.lejdd.fr/rss.xml",
        "National Sunday weekly, right",
    ),
    _f(
        "FR",
        "fr",
        "Reporterre",
        "national",
        "https://reporterre.net/spip.php?page=backend",
        "Independent ecology outlet",
    ),
    # France: regional press
    _f(
        "FR",
        "fr",
        "Ouest-France",
        "regional",
        "https://www.ouest-france.fr/rss/une",
        "Largest regional daily (West)",
    ),
    _f(
        "FR",
        "fr",
        "La Dépêche du Midi",
        "regional",
        "https://www.ladepeche.fr/rss.xml",
        "Regional daily (South-West)",
    ),
    _f(
        "FR",
        "fr",
        "Sud Ouest",
        "regional",
        "https://www.sudouest.fr/rss.xml",
        "Regional daily (Nouvelle-Aquitaine)",
    ),
    _f(
        "FR",
        "fr",
        "Le Progrès",
        "regional",
        "https://www.leprogres.fr/rss",
        "Regional daily (Lyon, Rhône-Alpes)",
    ),
    _f(
        "FR",
        "fr",
        "Dernières Nouvelles d'Alsace",
        "regional",
        "https://www.dna.fr/rss",
        "Regional daily (Alsace)",
    ),
    _f(
        "FR",
        "fr",
        "Midi Libre",
        "regional",
        "https://www.midilibre.fr/rss.xml",
        "Regional daily (Occitanie)",
    ),
    # Belgium, Switzerland, Canada
    _f(
        "BE",
        "fr",
        "RTBF",
        "public_service",
        "https://rss.rtbf.be/article/rss/rtbfinfo_homepage.xml",
        "Belgian public broadcaster",
    ),
    _f(
        "BE",
        "fr",
        "La Libre Belgique",
        "national",
        "https://www.lalibre.be/arc/outboundfeeds/rss/?outputType=xml",
        "Belgian national daily",
    ),
    _f(
        "CH",
        "fr",
        "RTS",
        "public_service",
        "https://www.rts.ch/info/?format=rss/news",
        "Swiss French-language public broadcaster",
    ),
    _f(
        "CA",
        "fr",
        "Radio-Canada",
        "public_service",
        "https://ici.radio-canada.ca/rss/4159",
        "Canadian French-language public broadcaster",
    ),
    # International
    _f(
        "GB",
        "en",
        "BBC News (World)",
        "international",
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "UK public broadcaster, world news",
    ),
    _f(
        "DE",
        "en",
        "Deutsche Welle (World)",
        "international",
        "https://rss.dw.com/xml/rss-en-world",
        "German public international broadcaster",
    ),
    _f(
        "INT",
        "fr",
        "Euronews",
        "international",
        "https://fr.euronews.com/rss",
        "European news channel, French edition",
    ),
    _f(
        "INT",
        "fr",
        "ONU Info",
        "international",
        "https://news.un.org/feed/subscribe/fr/news/all/rss.xml",
        "United Nations news service",
    ),
    _f(
        "EU",
        "fr",
        "Commission européenne (presse)",
        "international",
        "https://ec.europa.eu/commission/presscorner/api/rss?language=fr",
        "European Commission press releases",
    ),
)


async def seed_news_feeds(session: AsyncSession) -> int:
    """Insert the missing seed feeds; return how many were inserted.

    Idempotent by ``url``: an existing row is left untouched, so a feed an editor
    deactivated or edited is never reactivated nor overwritten.
    """
    rows = [
        {
            "country": f.country,
            "lang": f.lang,
            "publisher": f.publisher,
            "kind": f.kind,
            "url": f.url,
            "domain": registrable_domain(f.url),
            "updated_by": "seed",
        }
        for f in SEED_FEEDS
    ]
    result = await session.execute(
        pg_insert(NewsFeed)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["url"])
        .returning(NewsFeed.id)
    )
    return len(result.all())
