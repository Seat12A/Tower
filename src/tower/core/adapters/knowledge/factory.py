"""Builds the Atlas from settings (adapters with a key only, DB source cache, registry).

Adapters are per-process singletons (module-level dict) so their httpx clients are
reused; the registry snapshot is reloaded at every call (one ``SELECT``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tower.core.adapters.knowledge.atlas import Atlas
    from tower.core.config import Settings
    from tower.core.db.database import Database

__all__ = ["build_atlas", "close_atlas_adapters"]

_SINGLETONS: dict[str, Any] = {}


def _singleton(name: str, build: Any) -> Any:
    if name not in _SINGLETONS:
        _SINGLETONS[name] = build()
    return _SINGLETONS[name]


async def close_atlas_adapters() -> None:
    """Close the shared httpx clients (process shutdown, tests)."""
    adapters = list(_SINGLETONS.values())
    _SINGLETONS.clear()
    for adapter in adapters:
        closer = getattr(adapter, "aclose", None)
        if closer is not None:
            await closer()


async def build_atlas(settings: Settings, db: Database) -> Atlas:
    from sqlalchemy import select

    from tower.core.adapters.http_fetcher import HttpFetcher
    from tower.core.adapters.knowledge.atlas import Atlas, RegistryInfo
    from tower.core.adapters.knowledge.brave import BraveSearch
    from tower.core.adapters.knowledge.cache import DbSourceCache
    from tower.core.adapters.knowledge.factcheck_tools import FactCheckToolsSource
    from tower.core.adapters.knowledge.insee import InseeSource, parse_series_config
    from tower.core.adapters.knowledge.legifrance import LegifranceSource
    from tower.core.adapters.knowledge.wikidata import WikidataSource
    from tower.core.adapters.knowledge.wikipedia import WikipediaSource
    from tower.core.models.factcheck import SourceRegistryEntry
    from tower.core.ports.knowledge import KnowledgeSource

    cache = _singleton("cache", lambda: DbSourceCache(db))
    common: dict[str, Any] = {
        "user_agent": settings.http_user_agent,
        "timeout": settings.atlas_http_timeout_s,
    }
    sources: dict[str, KnowledgeSource] = {
        "wikipedia": _singleton(
            "wikipedia",
            lambda: WikipediaSource(
                cache=cache,
                concurrency=settings.atlas_wikipedia_concurrency,
                cache_ttl_s=settings.atlas_cache_ttl_s,
                **common,
            ),
        ),
        "wikidata": _singleton(
            "wikidata",
            lambda: WikidataSource(cache=cache, cache_ttl_s=settings.atlas_cache_ttl_s, **common),
        ),
    }
    if settings.google_api_key is not None:
        key = settings.google_api_key.get_secret_value()
        sources["factcheck_tools"] = _singleton(
            "factcheck_tools", lambda: FactCheckToolsSource(api_key=key, cache=cache, **common)
        )
    if settings.piste_client_id is not None and settings.piste_client_secret is not None:
        client_id = settings.piste_client_id.get_secret_value()
        client_secret = settings.piste_client_secret.get_secret_value()
        sources["legifrance"] = _singleton(
            "legifrance",
            lambda: LegifranceSource(
                client_id=client_id,
                client_secret=client_secret,
                sandbox=settings.piste_sandbox,
                cache=cache,
                cache_ttl_s=settings.atlas_cache_ttl_s,
                **common,
            ),
        )
    series = parse_series_config(settings.insee_series)
    if series:
        sources["insee"] = _singleton(
            "insee",
            lambda: InseeSource(
                series=series, cache=cache, cache_ttl_s=settings.atlas_cache_ttl_s, **common
            ),
        )
    _add_lot6_sources(settings, db, cache, common, sources)
    search = None
    if settings.brave_api_key is not None:
        brave_key = settings.brave_api_key.get_secret_value()
        search = _singleton("brave", lambda: BraveSearch(api_key=brave_key, cache=cache, **common))
    fetcher = _singleton(
        "fetcher",
        lambda: HttpFetcher(
            user_agent=settings.http_user_agent, timeout=settings.atlas_http_timeout_s
        ),
    )
    async with db.session() as session:
        rows = (await session.execute(select(SourceRegistryEntry))).scalars().all()
        registry = {
            row.domain: RegistryInfo(
                domain=row.domain,
                publisher=row.publisher,
                tier=row.tier,
                kind=row.kind,
                country=row.country,
            )
            for row in rows
        }
        country_adapters, official_domains = await _country_registry(session)
    return Atlas(
        sources=sources,
        search=search,
        fetcher=fetcher,
        cache=cache,
        registry=registry,
        country_adapters=country_adapters,
        official_domains=official_domains,
    )


def _add_lot6_sources(
    settings: Settings, db: Database, cache: Any, common: dict[str, Any], sources: dict[str, Any]
) -> None:
    """Lot 6 sources: free statistics adapters (only with configured indicators), local
    mirrors (LEGI, news index: PostgreSQL, no HTTP), free on-demand legal APIs and GDELT."""
    from tower.core.adapters.knowledge.legi_mirror import LegiMirrorSource
    from tower.core.adapters.knowledge.news_index import NewsIndexSource
    from tower.core.adapters.knowledge.stat_common import parse_stat_indicators

    ttl = settings.atlas_cache_ttl_s
    sources["legi_mirror"] = _singleton("legi_mirror", lambda: LegiMirrorSource(db))
    sources["news_index"] = _singleton("news_index", lambda: NewsIndexSource(db))
    indicators = parse_stat_indicators(settings.atlas_stat_indicators)
    if indicators:
        from tower.core.adapters.knowledge.eurostat import EurostatSource
        from tower.core.adapters.knowledge.imf import ImfSource
        from tower.core.adapters.knowledge.oecd import OecdSource
        from tower.core.adapters.knowledge.worldbank import WorldBankSource

        classes: dict[str, Any] = {
            "eurostat": EurostatSource,
            "worldbank": WorldBankSource,
            "oecd": OecdSource,
            "imf": ImfSource,
        }
        for name, cls in classes.items():
            mine = [i for i in indicators if i.source == name]
            if mine:
                sources[name] = _singleton(
                    name,
                    lambda cls=cls, mine=mine: cls(
                        indicators=mine, cache=cache, cache_ttl_s=ttl, **common
                    ),
                )
    enabled = {n.strip() for n in settings.atlas_on_demand_legal.split(",") if n.strip()}
    if "eurlex" in enabled:
        from tower.core.adapters.knowledge.eurlex import EurLexSource

        sources["eurlex"] = _singleton(
            "eurlex", lambda: EurLexSource(cache=cache, cache_ttl_s=ttl, **common)
        )
    if "fedlex" in enabled:
        from tower.core.adapters.knowledge.fedlex import FedlexSource

        sources["fedlex"] = _singleton(
            "fedlex", lambda: FedlexSource(cache=cache, cache_ttl_s=ttl, **common)
        )
    if "canada_laws" in enabled:
        from tower.core.adapters.knowledge.canada_laws import CanadaLawsSource

        sources["canada_laws"] = _singleton(
            "canada_laws", lambda: CanadaLawsSource(cache=cache, cache_ttl_s=ttl, **common)
        )
    if settings.atlas_gdelt_enabled:
        from tower.core.adapters.knowledge.gdelt import GdeltSource

        sources["gdelt"] = _singleton("gdelt", lambda: GdeltSource(cache=cache, **common))


async def _country_registry(
    session: Any,
) -> tuple[dict[tuple[str, str], list[str]], dict[str, list[str]]]:
    """``jurisdiction_sources`` → (country, type) adapters and country official domains."""
    from sqlalchemy import select

    from tower.core.models.atlas import JurisdictionSource

    rows = (
        await session.execute(
            select(JurisdictionSource)
            .where(JurisdictionSource.active.is_(True))
            .order_by(JurisdictionSource.id)
        )
    ).scalars()
    adapters: dict[tuple[str, str], list[str]] = {}
    domains: dict[str, list[str]] = {}
    for row in rows:
        if row.adapter:
            adapters.setdefault((row.country, row.source_type), []).append(row.adapter)
        if row.domain and row.tier == "primary":
            domains.setdefault(row.country, []).append(row.domain)
    return adapters, domains
