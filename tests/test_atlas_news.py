"""Atlas news index: RSS parsing, polling, clustering, purge, index search, GDELT, seed."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import structlog
from sqlalchemy import select, text

from tower.core.adapters.knowledge.cache import DbSourceCache
from tower.core.adapters.knowledge.gdelt import GdeltSource, MinInterval
from tower.core.adapters.knowledge.http import SourceHttp
from tower.core.adapters.knowledge.news_index import NewsIndexSource, search_terms
from tower.core.adapters.knowledge.rss import UnsafeXml, fetch_feed, parse_feed
from tower.core.atlas_seed_feeds import SEED_FEEDS, seed_news_feeds
from tower.core.config import Settings
from tower.core.db.database import Database
from tower.core.domain import error_codes as ec
from tower.core.domain.factcheck.atlas import NewsFeedKind, valid_country
from tower.core.errors import get_reporter
from tower.core.factcheck import names
from tower.core.jobs.queue import JobRecord
from tower.core.models.atlas import NewsFeed, NewsItem
from tower.core.models.errors import ErrorReport
from tower.core.ports.knowledge import SourceQuery, SourceUnavailable, Tier
from tower.core.services.base import JobContext
from tower.services.atlas import news
from tower.services.atlas.news import enqueue_news_poll, news_poll, purge_news

FIXTURES = Path(__file__).parent / "fixtures" / "atlas_news"
UA = "Seat12A-Tower/test"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# --- parsing -------------------------------------------------------------------------------


def test_parse_rss2_strips_html_and_handles_missing_date() -> None:
    entries = parse_feed(fixture("rss2.xml"), max_items=10)
    assert [e.title for e in entries] == [
        "Budget 2027 : le gouvernement présente ses arbitrages",
        "Sans date",
    ]  # the blank title is dropped, the rel=self atom:link is not an item link
    first, second = entries
    assert first.lead == 'Le Premier ministre a détaillé jeudi les "économies" prévues.'
    assert "alert" not in first.lead and "<" not in first.lead
    assert first.published == datetime(2026, 11, 12, 8, 30, tzinfo=UTC)
    assert first.guid == "abc-1"
    assert second.published is None


def test_parse_atom_prefers_alternate_link_and_published() -> None:
    entries = parse_feed(fixture("atom.xml"), max_items=10)
    assert entries[0].url == "https://example.net/sport/bleus-2-1"
    assert entries[0].title == "Les Bleus l'emportent 2-1"
    assert entries[0].lead == "Victoire en match amical."
    assert entries[0].published == datetime(2026, 11, 13, 20, 15, tzinfo=UTC)
    assert entries[1].published == datetime(2026, 11, 13, 7, 0, tzinfo=UTC)  # <updated>
    assert entries[1].lead == "Plain content."


def test_parse_rdf_and_caps() -> None:
    entries = parse_feed(fixture("rdf.xml"), max_items=10)
    assert len(entries) == 1
    assert entries[0].lead == "Les délégués ont adopté un texte."
    assert entries[0].published == datetime(2026, 11, 11, 12, tzinfo=UTC)
    assert len(parse_feed(fixture("rss2.xml"), max_items=1)) == 1
    long_lead = (
        b"<rss><channel><item><title>T</title><link>https://a.example/x</link>"
        b"<description>" + b"word " * 400 + b"</description></item></channel></rss>"
    )
    assert len(parse_feed(long_lead, max_items=5)[0].lead) <= 600


def test_parse_rejects_entity_declaration_and_oversize() -> None:
    with pytest.raises(UnsafeXml):
        parse_feed(fixture("evil.xml"), max_items=10)
    with pytest.raises(UnsafeXml):
        parse_feed(b"<rss>" + b" " * 100, max_items=10, max_bytes=50)


# --- fetch (conditional GET) ---------------------------------------------------------------


def http_for(handler: Callable[[httpx.Request], httpx.Response]) -> SourceHttp:
    return SourceHttp("news", timeout=5, user_agent=UA, transport=httpx.MockTransport(handler))


class _F:
    url = "https://example.org/feed.xml"


async def test_fetch_feed_conditional_get() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(
            200,
            content=fixture("rss2.xml"),
            headers={"ETag": '"v1"', "Last-Modified": "Thu, 12 Nov 2026 10:00:00 GMT"},
        )

    http = http_for(handler)
    first = await fetch_feed(http, _F(), etag=None, last_modified=None)
    assert first.status == 200 and len(first.entries) == 2
    assert first.etag == '"v1"' and first.last_modified == "Thu, 12 Nov 2026 10:00:00 GMT"
    assert "if-none-match" not in seen[0].headers
    second = await fetch_feed(http, _F(), etag=first.etag, last_modified=first.last_modified)
    assert second.not_modified and second.entries == []
    assert seen[1].headers["if-modified-since"] == "Thu, 12 Nov 2026 10:00:00 GMT"
    await http.aclose()


# --- poll ----------------------------------------------------------------------------------


@pytest.fixture
async def news_db(db: Database) -> AsyncIterator[Database]:
    async with db.transaction() as session:
        await session.execute(text("TRUNCATE news_index, news_feeds RESTART IDENTITY CASCADE"))
    yield db
    news.set_http_factory(None)


def ctx_for(db: Database, settings: Settings) -> JobContext:
    job = JobRecord(
        id=1,
        queue=names.ATLAS_QUEUE,
        kind=names.JOB_NEWS_POLL,
        payload={},
        priority=0,
        attempts=1,
        max_attempts=1,
        dedupe_key=None,
        created_at=datetime.now(UTC),
    )
    return JobContext(job=job, db=db, settings=settings, log=structlog.get_logger("test"))


def rss(items: list[tuple[str, str, str, datetime]]) -> bytes:
    body = "".join(
        f"<item><title>{t}</title><link>{u}</link><description>{d}</description>"
        f"<pubDate>{format_datetime(p)}</pubDate></item>"
        for t, u, d, p in items
    )
    return f"<?xml version='1.0'?><rss version='2.0'><channel>{body}</channel></rss>".encode()


async def add_feed(db: Database, url: str, publisher: str, *, country: str = "FR") -> int:
    async with db.transaction() as session:
        feed = NewsFeed(
            country=country,
            lang="fr",
            publisher=publisher,
            kind="national",
            url=url,
            domain=url.split("/")[2],
        )
        session.add(feed)
        await session.flush()
        return feed.id


def serve(routes: dict[str, httpx.Response | Callable[[], httpx.Response]]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        route = routes.get(str(request.url))
        if route is None:
            return httpx.Response(404)
        return route() if callable(route) else route

    news.set_http_factory(
        lambda s: SourceHttp(
            "news",
            timeout=5,
            user_agent=UA,
            transport=httpx.MockTransport(handler),
        )
    )


WIRE_LEAD = (
    "Le gouvernement a annoncé vendredi un plan de soutien de deux milliards d'euros destiné "
    "aux agriculteurs touchés par la sécheresse, avec des aides directes versées avant la fin "
    "de l'année et un report des cotisations sociales pour les exploitations les plus fragiles."
)


async def items_of(db: Database) -> list[Any]:
    async with db.session() as session:
        return list((await session.execute(select(NewsItem).order_by(NewsItem.id))).scalars())


async def test_poll_inserts_dedupes_clusters_and_isolates_failures(
    news_db: Database, settings: Settings
) -> None:
    now = datetime.now(UTC)
    wire = [
        ("Sécheresse : deux milliards d'euros pour les agriculteurs", "https://a.example/s1"),
        (
            "Sécheresse : deux milliards d'euros pour les agriculteurs",
            "https://b.example/s2?utm_source=x",
        ),
        ("Sécheresse, deux milliards d'euros pour les agriculteurs", "https://c.example/s3"),
    ]
    other = ("Le festival de Cannes dévoile son affiche", "https://c.example/cannes")
    feeds = {
        "https://a.example/rss": rss(
            [(wire[0][0], wire[0][1], WIRE_LEAD, now - timedelta(minutes=50))]
        ),
        "https://b.example/rss": rss(
            [
                (wire[1][0], wire[1][1], WIRE_LEAD, now - timedelta(minutes=40)),
                # same article under two tracking variants: stored once
                (wire[1][0], "https://b.example/s2?utm_campaign=y", WIRE_LEAD, now),
                (
                    "Trop ancien pour l'index",
                    "https://b.example/old",
                    "Vieux.",
                    now - timedelta(days=200),
                ),
            ]
        ),
        "https://c.example/rss": rss(
            [
                (wire[2][0], wire[2][1], WIRE_LEAD, now - timedelta(minutes=30)),
                (
                    other[0],
                    other[1],
                    "Les films en compétition sont connus.",
                    now - timedelta(minutes=20),
                ),
            ]
        ),
    }
    ids = {
        u: await add_feed(news_db, u, u.split("/")[2]) for u in [*feeds, "https://bad.example/rss"]
    }
    serve(
        {
            **{
                u: httpx.Response(200, content=c, headers={"ETag": '"e"'}) for u, c in feeds.items()
            },
            "https://bad.example/rss": httpx.Response(503),
        }
    )
    await news_poll(ctx_for(news_db, settings))

    rows = await items_of(news_db)
    assert len(rows) == 4  # old item ignored, utm variant deduped
    s1, s2, s3 = (
        next(r for r in rows if r.url.endswith(p)) for p in ("/s1", "/s2?utm_source=x", "/s3")
    )
    assert s1.cluster_id == s1.id == s2.cluster_id == s3.cluster_id
    cannes = next(r for r in rows if r.url.endswith("/cannes"))
    assert cannes.cluster_id == cannes.id
    assert s2.canonical_url == "https://b.example/s2"
    assert s1.summary and s1.country == "FR" and s1.origin == "rss"

    async with news_db.session() as session:
        feed_rows = {f.url: f for f in (await session.execute(select(NewsFeed))).scalars()}
    assert feed_rows["https://a.example/rss"].etag == '"e"'
    assert feed_rows["https://a.example/rss"].failures == 0
    bad = feed_rows["https://bad.example/rss"]
    assert bad.failures == 1 and bad.last_status == 503
    assert ids["https://bad.example/rss"] == bad.id
    await get_reporter().flush()
    async with news_db.session() as session:
        codes = list((await session.execute(select(ErrorReport.code))).scalars())
    assert codes == [ec.ATL_FEED_FAILED.code]

    # second poll: nothing new, failure counted again (not captured: neither 1st nor 10th)
    await news_poll(ctx_for(news_db, settings))
    assert len(await items_of(news_db)) == 4
    async with news_db.session() as session:
        failures = (
            await session.execute(select(NewsFeed.failures).where(NewsFeed.id == bad.id))
        ).scalar()
    assert failures == 2


async def test_poll_unsafe_feed_is_rejected_and_counted(
    news_db: Database, settings: Settings
) -> None:
    feed_id = await add_feed(news_db, "https://evil.example/rss", "Evil")
    serve({"https://evil.example/rss": httpx.Response(200, content=fixture("evil.xml"))})
    await news_poll(ctx_for(news_db, settings))
    assert await items_of(news_db) == []
    async with news_db.session() as session:
        feed = (await session.execute(select(NewsFeed).where(NewsFeed.id == feed_id))).scalar_one()
    assert feed.failures == 1
    await get_reporter().flush()
    async with news_db.session() as session:
        codes = list((await session.execute(select(ErrorReport.code))).scalars())
    assert codes == [ec.ATL_UNSAFE_XML.code]


async def test_poll_skips_inactive_feeds_and_honours_304(
    news_db: Database, settings: Settings
) -> None:
    await add_feed(news_db, "https://a.example/rss", "A")
    off = await add_feed(news_db, "https://off.example/rss", "Off")
    async with news_db.transaction() as session:
        await session.execute(
            text("UPDATE news_feeds SET active = false WHERE id = :i"), {"i": off}
        )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.headers.get("if-none-match") == '"e"':
            return httpx.Response(304)
        return httpx.Response(
            200,
            content=rss(
                [
                    (
                        "Un titre assez long pour exister",
                        "https://a.example/1",
                        "Lead.",
                        datetime.now(UTC),
                    )
                ]
            ),
            headers={"ETag": '"e"'},
        )

    news.set_http_factory(
        lambda s: SourceHttp(
            "news", timeout=5, user_agent=UA, transport=httpx.MockTransport(handler)
        )
    )
    await news_poll(ctx_for(news_db, settings))
    await news_poll(ctx_for(news_db, settings))
    assert calls == ["https://a.example/rss"] * 2
    assert len(await items_of(news_db)) == 1
    async with news_db.session() as session:
        status = (
            await session.execute(
                select(NewsFeed.last_status).where(NewsFeed.url == "https://a.example/rss")
            )
        ).scalar()
    assert status == 304


async def test_purge_news_batches(
    news_db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    await add_feed(news_db, "https://a.example/rss", "A")
    old = datetime.now(UTC) - timedelta(days=settings.atlas_news_retention_days + 5)
    async with news_db.transaction() as session:
        for i in range(5):
            when = old if i < 3 else datetime.now(UTC)
            await session.execute(
                text(
                    "INSERT INTO news_index (origin, url, canonical_url, publisher, domain, lang,"
                    " title, published_at, simhash, simhash_b0, simhash_b1, simhash_b2, simhash_b3)"
                    " VALUES ('rss', :u, :u, 'P', 'a.example', 'fr', 't', :w, 0, 0, 0, 0, 0)"
                ),
                {"u": f"https://a.example/{i}", "w": when},
            )
    monkeypatch.setattr(news, "PURGE_BATCH", 2)
    async with news_db.transaction() as session:
        assert await purge_news(session, settings) == 3
    assert len(await items_of(news_db)) == 2


async def test_enqueue_news_poll_dedupes_per_slot(news_db: Database, settings: Settings) -> None:
    async with news_db.transaction() as session:
        assert await enqueue_news_poll(session, settings) == 1
        assert await enqueue_news_poll(session, settings) == 0
    async with news_db.session() as session:
        rows = (await session.execute(text("SELECT kind, queue, dedupe_key FROM jobs"))).all()
    assert len(rows) == 1
    assert rows[0].kind == names.JOB_NEWS_POLL and rows[0].queue == names.ATLAS_QUEUE
    assert rows[0].dedupe_key.startswith("atlas:news:")
    assert news._slot(datetime(2026, 11, 14, 10, 29, 59, tzinfo=UTC)) == "2026-11-14T10:15"


# --- NewsIndexSource -------------------------------------------------------------------------


async def test_news_index_source_one_evidence_per_cluster(
    news_db: Database, settings: Settings
) -> None:
    now = datetime.now(UTC)
    lead = WIRE_LEAD
    await add_feed(news_db, "https://a.example/rss", "Outlet A")
    await add_feed(news_db, "https://b.example/rss", "Outlet B", country="BE")
    t = "Sécheresse : deux milliards d'euros pour les agriculteurs"
    serve(
        {
            "https://a.example/rss": httpx.Response(
                200,
                content=rss(
                    [
                        (t, "https://a.example/s1", lead, now - timedelta(hours=5)),
                        (
                            "Réforme des retraites : le texte adopté",
                            "https://a.example/r",
                            "Vote au Sénat.",
                            now - timedelta(days=2),
                        ),
                        (
                            "Ancien article sur la sécheresse",
                            "https://a.example/old",
                            "Archive.",
                            now - timedelta(days=40),
                        ),
                    ]
                ),
            ),
            "https://b.example/rss": httpx.Response(
                200, content=rss([(t, "https://b.example/s2", lead, now - timedelta(hours=4))])
            ),
        }
    )
    await news_poll(ctx_for(news_db, settings))
    source = NewsIndexSource(news_db)
    assert source.name == "news_index" and source.reliability_tier is Tier.SECONDARY

    hits = await source.lookup(SourceQuery(text="les agriculteurs et la sécheresse", lang="fr"))
    assert hits[0].url == "https://a.example/s1"  # earliest of the cluster
    drought = hits[0]
    assert drought.extra["cluster_size"] == "2" and drought.extra["origin"] == "rss"
    assert drought.publisher == "Outlet A" and drought.license == "headline and link only"
    assert drought.text.startswith(f"Outlet A, {(now - timedelta(hours=5)).date().isoformat()}: ")
    assert drought.tier is Tier.SECONDARY and drought.lang == "fr"
    assert sum(h.extra["cluster_id"] == drought.extra["cluster_id"] for h in hits) == 1
    assert all("/old" not in h.url for h in hits)  # outside the 30-day window

    # as_of in the past: the recent wire did not exist yet, the 40-day-old item is in range
    past = await source.lookup(
        SourceQuery(text="sécheresse", lang="fr", as_of=now - timedelta(days=20))
    )
    assert [h.url for h in past] == ["https://a.example/old"]
    # jurisdiction narrows first then widens
    be = await source.lookup(
        SourceQuery(text="agriculteurs sécheresse retraites", lang="fr", jurisdiction="BE", limit=5)
    )
    assert be[0].url == "https://a.example/s1"  # cluster root is A even if B matched first
    assert len(be) == 2
    assert await source.lookup(SourceQuery(text="le la de", lang="fr")) == []
    assert search_terms("Le budget de la sécurité sociale en 2026") == (
        "sécurité OR sociale OR budget"
    )


# --- GDELT -----------------------------------------------------------------------------------

GDELT_BODY = {
    "articles": [
        {
            "url": "https://www.midilibre.fr/2026/11/13/a.php",
            "url_mobile": "",
            "title": "Titre  avec  espaces , bizarres ",
            "seendate": "20261113T180000Z",
            "socialimage": "https://img/x.png",
            "domain": "midilibre.fr",
            "language": "French",
            "sourcecountry": "France",
        },
        {
            "url": "https://www.midilibre.fr/2026/11/13/a.php",
            "title": "Duplicate url",
            "seendate": "20261113T180000Z",
            "domain": "midilibre.fr",
            "language": "French",
            "sourcecountry": "France",
        },
        {"url": "javascript:alert(1)", "title": "bad", "domain": "x"},
    ]
}


async def test_gdelt_parsing_cache_and_min_interval(news_db: Database) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=json.dumps(GDELT_BODY))

    now = [100.0]
    slept: list[float] = []

    async def sleep(delay: float) -> None:
        slept.append(delay)
        now[0] += delay

    source = GdeltSource(
        user_agent=UA,
        timeout=5,
        cache=DbSourceCache(news_db),
        transport=httpx.MockTransport(handler),
        interval=MinInterval(5.0, clock=lambda: now[0], sleep=sleep),
    )
    query = SourceQuery(text="vaccins autisme étude", lang="fr", jurisdiction="FR", limit=5)
    hits = await source.lookup(query)
    assert len(hits) == 1
    hit = hits[0]
    assert hit.source == "gdelt" and hit.tier is Tier.TERTIARY
    assert hit.text == "midilibre.fr, 2026-11-13: Titre avec espaces , bizarres"
    assert hit.published_at == datetime(2026, 11, 13, 18, tzinfo=UTC)
    assert hit.lang == "fr" and hit.extra["country"] == "France"
    params = dict(seen[0].url.params)
    assert params["mode"] == "artlist" and params["format"] == "json"
    assert "sourcelang:french" in params["query"] and "sourcecountry:FR" in params["query"]
    assert slept == []  # first call: no wait

    assert len(await source.lookup(query)) == 1  # cache hit
    assert len(seen) == 1 and slept == []

    other = SourceQuery(text="inflation prix énergie", lang="fr")
    await source.lookup(other)
    assert len(seen) == 2 and len(slept) == 1 and 0 < slept[0] <= 5.0
    assert (
        await source.lookup(
            SourceQuery(text="vaccins", lang="fr", as_of=datetime(2020, 1, 1, tzinfo=UTC))
        )
        == []
    )
    await source.aclose()


async def test_gdelt_rate_limited_and_bad_json() -> None:
    async def sleep(_: float) -> None:
        return None

    def rate_limited(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="Please limit requests to one every 5 seconds")

    source = GdeltSource(
        user_agent=UA,
        timeout=5,
        transport=httpx.MockTransport(rate_limited),
        sleep=sleep,
        interval=MinInterval(0, sleep=sleep),
    )
    with pytest.raises(SourceUnavailable) as err:
        await source.lookup(SourceQuery(text="vaccins autisme", lang="fr"))
    assert err.value.code is ec.ATL_RATE_LIMITED
    await source.aclose()

    bad = GdeltSource(
        user_agent=UA,
        timeout=5,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, text="Your query is too short")
        ),
        interval=MinInterval(0, sleep=sleep),
    )
    with pytest.raises(SourceUnavailable) as err2:
        await bad.lookup(SourceQuery(text="vaccins autisme", lang="fr"))
    assert err2.value.code is ec.ATL_BAD_RESPONSE
    await bad.aclose()


# --- seed ------------------------------------------------------------------------------------


async def test_seed_news_feeds_is_idempotent_and_keeps_editor_choices(news_db: Database) -> None:
    async with news_db.transaction() as session:
        assert await seed_news_feeds(session) == len(SEED_FEEDS)
    async with news_db.transaction() as session:
        assert await seed_news_feeds(session) == 0
    first = SEED_FEEDS[0]
    async with news_db.transaction() as session:
        await session.execute(
            text("UPDATE news_feeds SET active = false WHERE url = :u"), {"u": first.url}
        )
        await session.execute(
            text("DELETE FROM news_feeds WHERE url = :u"), {"u": SEED_FEEDS[1].url}
        )
    async with news_db.transaction() as session:
        assert await seed_news_feeds(session) == 1  # only the deleted one comes back
    async with news_db.session() as session:
        active = (
            await session.execute(select(NewsFeed.active).where(NewsFeed.url == first.url))
        ).scalar()
    assert active is False  # never reactivated


def test_seed_list_is_valid_and_balanced() -> None:
    kinds = {k.value for k in NewsFeedKind}
    assert len({f.url for f in SEED_FEEDS}) == len(SEED_FEEDS)
    assert all(f.kind in kinds and valid_country(f.country) and f.reason for f in SEED_FEEDS)
    assert all(f.url.startswith("https://") for f in SEED_FEEDS)
    present = {f.kind for f in SEED_FEEDS}
    assert {"public_service", "national", "regional", "international"} <= present
    assert 20 <= len(SEED_FEEDS) <= 40
