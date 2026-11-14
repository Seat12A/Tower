"""Atlas orchestration with fake sources, search and fetcher; registry seed on the real DB."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import text

from tower.core.adapters.knowledge.atlas import (
    Atlas,
    AtlasQuery,
    AtlasRequest,
    RegistryInfo,
    canonical_url,
    jurisdiction_languages,
    registrable_domain,
)
from tower.core.db.database import Database
from tower.core.domain import error_codes as ec
from tower.core.errors import ErrorReporter, set_reporter
from tower.core.factcheck.registry_seed import SEED, seed_registry
from tower.core.models.factcheck import SourceRegistryEntry
from tower.core.ports.knowledge import (
    Evidence,
    SourceCapabilities,
    SourceQuery,
    SourceUnavailable,
    Tier,
)
from tower.core.ports.web import FetchedPage, FetchError, SearchResult


def reg(
    domain: str, tier: str, country: str | None = "FR", publisher: str | None = None
) -> RegistryInfo:
    return RegistryInfo(domain, publisher or domain, tier, "institution", country)


REGISTRY = {
    r.domain: r
    for r in (
        reg("gouv.fr", "primary", publisher="French government"),
        reg("insee.fr", "primary"),
        reg("legifrance.gouv.fr", "primary"),
        reg("europa.eu", "primary", "EU"),
        reg("who.int", "primary", "INT"),
        reg("afp.com", "secondary"),
        reg("wikipedia.org", "tertiary", "INT"),
        reg("fakenews.example", "avoid", None),
    )
}


def ev(source: str, url: str, text_: str = "text", **kw: Any) -> Evidence:
    return Evidence(
        source=source,
        stable_id=kw.pop("stable_id", url),
        url=url,
        text=text_,
        lang="fr",
        license="x",
        sha256="0" * 64,
        **kw,
    )


class FakeSource:
    capabilities = SourceCapabilities()
    reliability_tier = Tier.PRIMARY
    cost_per_call_usd_micros = 0
    license = "x"

    def __init__(
        self, name: str, results: Sequence[Evidence] = (), error: SourceUnavailable | None = None
    ) -> None:
        self.name = name
        self.results = list(results)
        self.error = error
        self.queries: list[SourceQuery] = []

    async def lookup(self, query: SourceQuery) -> Sequence[Evidence]:
        self.queries.append(query)
        if self.error:
            raise self.error
        return self.results


class FakeSearch:
    def __init__(
        self,
        results: dict[tuple[str, bool], list[SearchResult]] | None = None,
        error: SourceUnavailable | None = None,
    ) -> None:
        self.results = results or {}
        self.error = error
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def search(
        self,
        query: str,
        *,
        limit: int,
        lang: str | None = None,
        country: str | None = None,
        include_domains: Sequence[str] = (),
        exclude_domains: Sequence[str] = (),
    ) -> Sequence[SearchResult]:
        self.calls.append((query, tuple(include_domains)))
        if self.error:
            raise self.error
        return self.results.get((query, bool(include_domains)), [])


class FakeFetcher:
    def __init__(self, pages: dict[str, FetchedPage | FetchError]) -> None:
        self.pages = pages
        self.fetched: list[str] = []

    async def fetch(self, url: str) -> FetchedPage:
        self.fetched.append(url)
        page = self.pages.get(url)
        if page is None:
            raise FetchError("nope", code=ec.WEB_FETCH_FAILED, url=url)
        if isinstance(page, FetchError):
            raise page
        return replace(page, url=url, final_url=url)


def page(url: str, body: str, ctype: str = "text/html") -> FetchedPage:
    return FetchedPage(
        url=url,
        final_url=url,
        status=200,
        content_type=ctype,
        text=body,
        sha256="a" * 64,
        fetched_at=datetime.now(UTC),
    )


def html(title: str, body: str) -> str:
    return f"<html><head><title>{title}</title></head><body><p>{body}</p></body></html>"


def sr(url: str) -> SearchResult:
    return SearchResult(url=url, title=f"t {url}", snippet="s")


def request(**kw: Any) -> AtlasRequest:
    base: dict[str, Any] = {
        "claim_text": "Le chômage a baissé",
        "claim_lang": "fr",
        "claim_type": "statistic",
        "jurisdictions": ("FR",),
        "queries": (),
        "entity_lookups": (),
        "max_web_searches": 4,
    }
    base.update(kw)
    return AtlasRequest(**base)


def q(text_: str, target: str, side: str = "confirm", lang: str = "fr") -> AtlasQuery:
    return AtlasQuery(query=text_, lang=lang, source_type="x", target=target, side=side)


def atlas(**kw: Any) -> Atlas:
    kw.setdefault("sources", {})
    kw.setdefault("search", None)
    kw.setdefault("fetcher", None)
    kw.setdefault("cache", None)
    kw.setdefault("registry", REGISTRY)
    return Atlas(**kw)


# --- pure helpers ------------------------------------------------------------------------


def test_registrable_domain_and_canonical_url() -> None:
    assert registrable_domain("https://www.insee.fr/fr/x?y=1") == "insee.fr"
    assert registrable_domain("https://WWW.Legifrance.gouv.fr:443/a") == "legifrance.gouv.fr"
    assert registrable_domain("news.bbc.co.uk") == "bbc.co.uk"
    assert registrable_domain("eur-lex.europa.eu") == "europa.eu"
    assert registrable_domain("localhost") == "localhost"
    assert (
        canonical_url("HTTPS://www.Example.com:443/a?utm_source=x&b=2&fbclid=z#frag")
        == "https://example.com/a?b=2"
    )
    assert canonical_url("https://x.org/a#f", keep_fragment=True) == "https://x.org/a#f"
    assert jurisdiction_languages(["CH", "DE"]) == ["fr", "de"]
    assert jurisdiction_languages(["US"]) == ["en"]


def test_tier_of_suffix_most_specific_wins() -> None:
    a = atlas()
    assert a.tier_of("https://www.sante.gouv.fr/x") == ("primary", "French government")
    assert a.tier_of("https://insee.fr")[0] == "primary"
    assert a.tier_of("https://legifrance.gouv.fr/a") == ("primary", "legifrance.gouv.fr")
    assert a.tier_of("eur-lex.europa.eu")[0] == "primary"
    assert a.tier_of("https://evil-gouv.fr/")[0] == "tertiary"
    assert a.tier_of("https://example.org") == ("tertiary", None)
    assert a.tier_of("https://x.fakenews.example/a")[0] == "avoid"
    custom = atlas(
        registry={
            "gouv.fr": reg("gouv.fr", "primary"),
            "blog.gouv.fr": reg("blog.gouv.fr", "tertiary"),
        }
    )
    assert custom.tier_of("a.blog.gouv.fr")[0] == "tertiary"


def test_primary_domains_by_jurisdiction_without_redundant_subdomains() -> None:
    a = atlas()
    assert a.primary_domains(["FR"]) == ["gouv.fr", "insee.fr", "europa.eu", "who.int"]
    assert a.primary_domains(["US"]) == ["europa.eu", "who.int"]


# --- collect -----------------------------------------------------------------------------


async def test_collect_order_and_documents() -> None:
    order: list[str] = []

    class Tracking(FakeSource):
        async def lookup(self, query: SourceQuery) -> Sequence[Evidence]:
            order.append(self.name)
            return await super().lookup(query)

    sources = {
        "factcheck_tools": Tracking(
            "factcheck_tools", [ev("factcheck_tools", "https://factuel.afp.com/1")]
        ),
        "wikidata": Tracking(
            "wikidata",
            [
                ev(
                    "wikidata",
                    "https://www.wikidata.org/wiki/Q1#P39",
                    stable_id="s1",
                    tier=Tier.TERTIARY,
                )
            ],
        ),
        "insee": Tracking(
            "insee",
            [ev("insee", "https://www.insee.fr/fr/statistiques/serie/1", tier=Tier.PRIMARY)],
        ),
        "legifrance": Tracking("legifrance"),
        "wikipedia": Tracking(
            "wikipedia",
            [ev("wikipedia", "https://fr.wikipedia.org/w/index.php?oldid=5", references=())],
        ),
    }
    search = FakeSearch(
        {("budget", True): [sr("https://www.sante.gouv.fr/p"), sr("https://x.org/b")]}
    )
    fetcher = FakeFetcher(
        {
            "https://www.sante.gouv.fr/p": page("u", html("Santé", "contenu officiel")),
            "https://x.org/b": page("u", html("X", "autre")),
        }
    )
    a = atlas(sources=sources, search=search, fetcher=fetcher, registry=REGISTRY)
    res = await a.collect(
        request(
            queries=(q("fc query", "fact_check"), q("encyclo", "encyclopedia"), q("budget", "web")),
            entity_lookups=("Emmanuel Macron",),
        )
    )
    assert order == ["factcheck_tools", "factcheck_tools", "wikidata", "insee", "wikipedia"]
    assert [d.adapter for d in res.documents] == [
        "factcheck_tools",
        "wikidata",
        "insee",
        "wikipedia",
        "brave",
        "brave",
    ]
    web = res.documents[-2]
    assert web.tier == "primary" and web.publisher == "French government"
    assert web.text == "contenu officiel" and web.title == "Santé"
    assert web.canonical_url == "https://sante.gouv.fr/p"
    assert res.documents[1].tier == "tertiary"  # adapter-declared
    assert res.documents[2].tier == "primary"
    assert res.documents[0].tier == "secondary"  # registry: factuel.afp.com → afp.com
    assert res.web_searches == 1 and res.paid_cost_usd_micros == 5000
    assert (
        set(res.adapters_used)
        == {"factcheck_tools", "wikidata", "insee", "wikipedia", "brave", "legifrance"}
        - {"legifrance"}
        | set()
    )


async def test_legifrance_only_for_french_legal_claims() -> None:
    lf = FakeSource(
        "legifrance", [ev("legifrance", "https://www.legifrance.gouv.fr/codes/article_lc/X")]
    )
    a = atlas(sources={"legifrance": lf})
    assert (await a.collect(request(claim_type="statistic"))).documents == []
    assert (
        await a.collect(request(claim_type="legal_text", jurisdictions=("DE",)))
    ).documents == []
    res = await a.collect(request(claim_type="procedure"))
    assert len(res.documents) == 1
    assert res.documents[0].tier == "primary"  # adapter-declared or registry (legifrance.gouv.fr)
    assert lf.queries[0].claim_type == "procedure" and lf.queries[0].jurisdiction == "FR"


async def test_unavailable_source_skipped_and_captured(db: Database) -> None:
    set_reporter(ErrorReporter(db, process="test"))
    broken = FakeSource(
        "factcheck_tools",
        error=SourceUnavailable("down", code=ec.ATL_SOURCE_UNAVAILABLE, source="factcheck_tools"),
    )
    wikidata = FakeSource(
        "wikidata", [ev("wikidata", "https://www.wikidata.org/wiki/Q1#P1", stable_id="a")]
    )
    res = await atlas(sources={"factcheck_tools": broken, "wikidata": wikidata}).collect(
        request(entity_lookups=("X",), claim_type="event")
    )
    assert res.skipped == {"factcheck_tools": "TWR-ATL-001"}
    assert len(res.documents) == 1
    assert "wikidata" in res.adapters_used and "factcheck_tools" not in res.adapters_used
    assert len(broken.queries) == 1  # the failing adapter is not retried inside the request
    from tower.core.errors import get_reporter

    await get_reporter().flush()
    async with db.session() as session:
        rows = (await session.execute(text("SELECT code FROM error_reports"))).scalars().all()
    assert "TWR-ATL-001" in rows


async def test_missing_sources_reported_as_not_configured() -> None:
    res = await atlas().collect(request(queries=(q("x", "web"),)))
    assert res.skipped["factcheck_tools"] == "TWR-ATL-004"
    assert res.skipped["brave"] == "TWR-ATL-004"


async def test_dedupe_by_canonical_url_and_max_documents() -> None:
    dupes = FakeSource(
        "factcheck_tools",
        [ev("factcheck_tools", f"https://factuel.afp.com/{i}?utm_source=a") for i in range(3)]
        + [ev("factcheck_tools", "https://www.factuel.afp.com/1")],
    )
    res = await atlas(sources={"factcheck_tools": dupes}).collect(request(max_documents=2))
    assert [d.canonical_url for d in res.documents] == [
        "https://factuel.afp.com/0",
        "https://factuel.afp.com/1",
    ]
    res = await atlas(sources={"factcheck_tools": dupes}).collect(request())
    assert len(res.documents) == 3  # 4 evidences, one duplicate


async def test_web_budget_symmetric_sides_and_broadening() -> None:
    queries = (
        q("c1", "web", "confirm"),
        q("c2", "web", "confirm"),
        q("c3", "web", "confirm"),
        q("r1", "web", "refute"),
        q("r2", "web", "refute"),
    )
    search = FakeSearch()  # nothing found: every restricted search wants to broaden
    a = atlas(search=search, fetcher=FakeFetcher({}))
    res = await a.collect(request(queries=queries, max_web_searches=4))
    searched = [c[0] for c in search.calls]
    assert res.web_searches == 4 == len(searched)
    assert searched == ["c1", "r1", "c2", "r2"]  # alternating; broadening never starves a side
    assert res.paid_cost_usd_micros == 4 * 5000
    # With a bigger budget the restricted-empty queries are rerun without domains.
    search2 = FakeSearch()
    res2 = await atlas(search=search2, fetcher=FakeFetcher({})).collect(
        request(queries=queries[:1] + queries[3:4], max_web_searches=4)
    )
    assert search2.calls[0] == ("c1", ("gouv.fr", "insee.fr", "europa.eu", "who.int"))
    assert search2.calls[1] == ("c1", ())
    assert [c[0] for c in search2.calls] == ["c1", "c1", "r1", "r1"]
    assert res2.web_searches == 4


async def test_web_fetches_two_per_search_and_handles_content_types() -> None:
    results = [
        sr("https://a.org/pdf"),
        sr("https://b.org/txt"),
        sr("https://c.org/html"),
        sr("https://d.org/4"),
    ]
    search = FakeSearch({("q", True): results, ("q", False): results})
    fetcher = FakeFetcher(
        {
            "https://a.org/pdf": page("u", "", "application/pdf"),
            "https://b.org/txt": page("u", "plain text body", "text/plain"),
            "https://c.org/html": page("u", html("C", "html body")),
            "https://d.org/4": page("u", html("D", "x")),
        }
    )
    res = await atlas(search=search, fetcher=fetcher).collect(request(queries=(q("q", "web"),)))
    assert fetcher.fetched == ["https://a.org/pdf", "https://b.org/txt"]  # ≤ 2 fetched per search
    assert [d.text for d in res.documents] == ["plain text body"]  # PDF keeps nothing


async def test_search_failure_skips_brave_but_keeps_other_documents() -> None:
    boom = FakeSearch(error=SourceUnavailable("rl", code=ec.ATL_RATE_LIMITED, source="brave"))
    wd = FakeSource(
        "wikidata", [ev("wikidata", "https://www.wikidata.org/wiki/Q1#P1", stable_id="a")]
    )
    res = await atlas(sources={"wikidata": wd}, search=boom, fetcher=FakeFetcher({})).collect(
        request(queries=(q("q", "web"),), entity_lookups=("X",))
    )
    assert res.skipped["brave"] == "TWR-ATL-002"
    assert res.web_searches == 0 and len(res.documents) == 1


async def test_wikipedia_references_followed_primary_first_and_bounded() -> None:
    refs = (
        "https://example.org/blog",
        "https://www.afp.com/dep",
        "https://www.insee.fr/stat",
        "https://www.sante.gouv.fr/a",
        "https://www.insee.fr/stat?utm_campaign=x",
        "https://bad.example/x",
    )
    wiki = FakeSource(
        "wikipedia",
        [ev("wikipedia", "https://fr.wikipedia.org/w/index.php?oldid=1", references=refs)],
    )
    fetcher = FakeFetcher(
        {
            "https://www.insee.fr/stat": page("u", html("INSEE", "chiffres")),
            "https://www.sante.gouv.fr/a": page("u", FetchedPage.__name__),  # plain html below
            "https://www.afp.com/dep": FetchError("robots", code=ec.WEB_ROBOTS_DISALLOWED, url="x"),
        }
    )
    fetcher.pages["https://www.sante.gouv.fr/a"] = page("u", html("Santé", "texte"))
    a = atlas(sources={"wikipedia": wiki}, fetcher=fetcher)
    res = await a.collect(request(queries=(q("enc", "encyclopedia"),), max_reference_fetches=3))
    assert fetcher.fetched == [
        "https://www.insee.fr/stat",
        "https://www.sante.gouv.fr/a",
        "https://www.afp.com/dep",
    ]  # primary first, deduped, tertiary skipped, bounded at 3
    assert [d.adapter for d in res.documents] == [
        "wikipedia",
        "wikipedia_reference",
        "wikipedia_reference",
    ]
    assert res.documents[1].tier == "primary" and res.documents[0].tier == "tertiary"
    assert wiki.queries[0].limit == 1 and wiki.queries[0].lang == "fr"


async def test_as_of_is_passed_to_sources() -> None:
    wd = FakeSource("wikidata")
    await atlas(sources={"wikidata": wd}).collect(
        request(entity_lookups=("X",), as_of=date(2020, 5, 1))
    )
    assert wd.queries[0].as_of == datetime(2020, 5, 1, tzinfo=UTC)


# --- registry seed -----------------------------------------------------------------------


def test_seed_shape() -> None:
    domains = [row[0] for row in SEED]
    assert len(domains) == len(set(domains))
    assert 40 <= len(SEED) <= 70
    assert {"gouv.fr", "insee.fr", "europa.eu", "wikipedia.org", "afp.com"} <= set(domains)
    assert next(r for r in SEED if r[0] == "wikipedia.org")[2] == "tertiary"


async def test_seed_registry_is_idempotent_and_never_overwrites(db: Database) -> None:
    async with db.transaction() as session:
        session.add(
            SourceRegistryEntry(
                domain="insee.fr",
                publisher="Custom",
                tier="secondary",
                kind="press",
                country="FR",
                origin="manual",
            )
        )
    async with db.transaction() as session:
        inserted, kept = await seed_registry(session)
    assert (inserted, kept) == (len(SEED) - 1, 1)
    async with db.transaction() as session:
        assert await seed_registry(session) == (0, len(SEED))
    async with db.session() as session:
        row = (
            await session.execute(
                text("SELECT publisher, tier, origin FROM source_registry WHERE domain='insee.fr'")
            )
        ).one()
        count = (await session.execute(text("SELECT count(*) FROM source_registry"))).scalar_one()
        origins = (
            (await session.execute(text("SELECT DISTINCT origin FROM source_registry ORDER BY 1")))
            .scalars()
            .all()
        )
    assert tuple(row) == ("Custom", "secondary", "manual")
    assert count == len(SEED)
    assert origins == ["manual", "seed"]


async def test_build_atlas_from_settings(db: Database) -> None:
    from pydantic import SecretStr

    from tests.conftest import make_settings
    from tower.core.adapters.knowledge.factory import build_atlas, close_atlas_adapters

    async with db.transaction() as session:
        await seed_registry(session)
    settings = make_settings(
        google_api_key=SecretStr("g"),
        brave_api_key=SecretStr("b"),
        piste_client_id=SecretStr("i"),
        piste_client_secret=SecretStr("s"),
        insee_series='{"chômage": "001688370"}',
    )
    try:
        built = await build_atlas(settings, db)
        assert set(built._sources) == {
            "wikipedia",
            "wikidata",
            "factcheck_tools",
            "legifrance",
            "insee",
            # lot 6, free and without key: local mirrors, on-demand legal APIs, GDELT
            "legi_mirror",
            "news_index",
            "eurlex",
            "fedlex",
            "canada_laws",
            "gdelt",
        }
        assert built._search is not None
        assert built.tier_of("https://www.insee.fr/x")[0] == "primary"
        again = await build_atlas(settings, db)
        assert again._sources["wikipedia"] is built._sources["wikipedia"]  # singleton
        await close_atlas_adapters()
        bare = await build_atlas(make_settings(), db)
        assert (
            set(bare._sources)
            == {
                "wikipedia",
                "wikidata",
                "legi_mirror",
                "news_index",
                "eurlex",
                "fedlex",
                "canada_laws",
                "gdelt",
            }
            and bare._search is None
        )
    finally:
        await close_atlas_adapters()


# --- lot 6: selection by jurisdiction, legal fallback, news ------------------------------


async def test_statistic_tries_the_country_then_the_international_adapters() -> None:
    names = ("insee", "eurostat", "worldbank", "oecd", "imf")
    sources = {n: FakeSource(n) for n in names}
    sources["eurostat"].results = [ev("eurostat", "https://ec.europa.eu/eurostat/x")]
    result = await atlas(sources=sources).collect(request(jurisdictions=("FR", "DE")))
    assert [q.jurisdiction for q in sources["eurostat"].queries] == ["FR", "DE"]
    assert len(sources["insee"].queries) == 1  # FR only (default country adapter)
    assert all(len(sources[n].queries) == 2 for n in ("worldbank", "oecd", "imf"))
    assert "eurostat" in result.adapters_used and len(result.documents) == 1


async def test_law_uses_the_mirror_first_and_legifrance_only_as_a_fallback() -> None:
    mirror = FakeSource("legi_mirror", [ev("legi_mirror", "https://www.legifrance.gouv.fr/a")])
    piste = FakeSource("legifrance")
    eurlex = FakeSource("eurlex")
    found = await atlas(
        sources={"legi_mirror": mirror, "legifrance": piste, "eurlex": eurlex}
    ).collect(request(claim_type="legal_text", jurisdictions=("FR", "EU")))
    assert len(mirror.queries) == 1 and piste.queries == []
    assert [q.jurisdiction for q in eurlex.queries] == ["EU"]
    assert "legi_mirror" in found.adapters_used
    empty_mirror = FakeSource("legi_mirror")
    await atlas(sources={"legi_mirror": empty_mirror, "legifrance": piste}).collect(
        request(claim_type="legal_text")
    )
    assert len(piste.queries) == 1  # the mirror found nothing: the PISTE API is asked


async def test_country_registry_overrides_the_defaults_and_adds_official_domains() -> None:
    fedlex = FakeSource("fedlex")
    custom = FakeSource("custom_ch")
    a = atlas(
        sources={"fedlex": fedlex, "custom_ch": custom},
        country_adapters={("CH", "law"): ["custom_ch"]},
        official_domains={"CH": ["admin.ch"]},
    )
    await a.collect(request(claim_type="legal_text", jurisdictions=("CH",)))
    assert len(custom.queries) == 1 and fedlex.queries == []
    assert "admin.ch" in a.primary_domains(["CH"])


async def test_news_comes_after_the_encyclopedia_and_gdelt_only_for_ongoing_situations() -> None:
    index = FakeSource("news_index", [ev("news_index", "https://www.lemonde.fr/a")])
    gdelt = FakeSource("gdelt")
    a = atlas(sources={"news_index": index, "gdelt": gdelt})
    await a.collect(request(claim_type="statistic"))
    assert len(index.queries) == 1 and gdelt.queries == []
    await a.collect(request(claim_type="ongoing_situation"))
    assert len(gdelt.queries) == 1
    await a.collect(request(claim_type="ongoing_situation", skip_encyclopedia=True))
    assert len(index.queries) == 2  # light check (E5): no news
