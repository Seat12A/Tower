"""Atlas adapters on ``httpx.MockTransport`` (no network); DB-backed cache tests."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from tower.core.adapters.knowledge.brave import BraveSearch
from tower.core.adapters.knowledge.cache import DbSourceCache, purge_expired
from tower.core.adapters.knowledge.factcheck_tools import FactCheckToolsSource
from tower.core.adapters.knowledge.html_text import MAX_OUTPUT_CHARS, extract_text
from tower.core.adapters.knowledge.insee import InseeSource, parse_series_config
from tower.core.adapters.knowledge.legifrance import LegifranceSource
from tower.core.adapters.knowledge.wikidata import WikidataSource
from tower.core.adapters.knowledge.wikipedia import WikipediaSource, reference_urls
from tower.core.db.database import Database
from tower.core.domain import error_codes as ec
from tower.core.ports.knowledge import SourceQuery, SourceUnavailable, Tier

UA = "Seat12A-Tower/test"
Handler = Callable[[httpx.Request], httpx.Response]


class DictCache:
    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {}
        self.ttls: list[int] = []

    @staticmethod
    def _k(adapter: str, request: Mapping[str, object]) -> str:
        return adapter + json.dumps(dict(request), sort_keys=True, default=str)

    async def get(self, adapter: str, request: Mapping[str, object]) -> dict[str, Any] | None:
        return self.data.get(self._k(adapter, request))

    async def put(
        self,
        adapter: str,
        request: Mapping[str, object],
        response: Mapping[str, Any],
        *,
        ttl_s: int,
    ) -> None:
        self.data[self._k(adapter, request)] = dict(response)
        self.ttls.append(ttl_s)


def counting(handler: Handler) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return httpx.MockTransport(wrapped), seen


async def no_sleep(_: float) -> None:
    return None


# --- html_text ---------------------------------------------------------------------------


def test_html_text_extracts_visible_text_and_meta() -> None:
    html = """<html><head><title> Budget 2026 </title>
    <meta name="description" content="Short summary">
    <meta property="article:published_time" content="2026-03-01T08:00:00Z">
    <script>var x = 1;</script><style>p{}</style></head>
    <body><nav>Menu</nav><header>Head</header>
    <article><h1>Title</h1><p>First paragraph.</p><p>Second   paragraph &amp; more.</p></article>
    <aside>Related</aside><form><input name=q>Search</form><footer>Foot</footer>
    <noscript>Enable JS</noscript></body></html>"""
    out = extract_text(html)
    assert out.title == "Budget 2026"
    assert out.description == "Short summary"
    assert out.published_at == datetime(2026, 3, 1, 8, tzinfo=UTC)
    assert out.text == "Title\n\nFirst paragraph.\n\nSecond paragraph & more."


def test_html_text_skip_classes_and_caps() -> None:
    html = '<p>Keep</p><div class="reflist"><ol><li>drop</li></ol><div>nested</div></div><p>End</p>'
    assert extract_text(html, skip_classes=["reflist"]).text == "Keep\n\nEnd"
    big = "<p>" + "a" * (MAX_OUTPUT_CHARS + 5000) + "</p>"
    assert len(extract_text(big).text) == MAX_OUTPUT_CHARS


# --- cache -------------------------------------------------------------------------------


async def test_db_cache_roundtrip_upsert_expiry_and_purge(db: Database) -> None:
    cache = DbSourceCache(db)
    req = {"q": "a", "n": 1}
    assert await cache.get("x", req) is None
    await cache.put("x", req, {"v": 1}, ttl_s=60)
    assert await cache.get("x", {"n": 1, "q": "a"}) == {"v": 1}  # canonical key order
    assert await cache.get("y", req) is None  # adapter is part of the key
    await cache.put("x", req, {"v": 2}, ttl_s=60)
    assert await cache.get("x", req) == {"v": 2}
    await cache.put("x", {"q": "old"}, {"v": 0}, ttl_s=60)
    async with db.transaction() as session:
        from sqlalchemy import text

        await session.execute(
            text(
                "UPDATE source_cache SET expires_at = now() - interval '1 hour' WHERE adapter='x'"
                " AND response->>'v' = '0'"
            )
        )
    assert await cache.get("x", {"q": "old"}) is None
    async with db.transaction() as session:
        assert await purge_expired(session, datetime.now(UTC), limit=10) == 1
    assert await cache.get("x", req) == {"v": 2}


# --- error mapping (shared by every adapter) ---------------------------------------------


def factcheck(handler: Handler, **kw: Any) -> tuple[FactCheckToolsSource, list[httpx.Request]]:
    transport, seen = counting(handler)
    source = FactCheckToolsSource(
        api_key="SECRET-KEY", user_agent=UA, timeout=2.0, transport=transport, sleep=no_sleep, **kw
    )
    return source, seen


Q = SourceQuery(text="le chômage a baissé", lang="fr")
REVIEW_JSON = {
    "claims": [
        {
            "text": "Le chômage a baissé de 50 %",
            "claimant": "Un ministre",
            "claimDate": "2026-01-10T00:00:00Z",
            "claimReview": [
                {
                    "publisher": {"name": "AFP Factuel", "site": "factuel.afp.com"},
                    "url": "https://factuel.afp.com/doc.1",
                    "title": "Non, le chômage n'a pas baissé de 50 %",
                    "reviewDate": "2026-01-15T10:00:00Z",
                    "textualRating": "Faux",
                    "languageCode": "fr",
                },
                {"url": "https://factuel.afp.com/doc.1"},
            ],
        },
        {"text": "no review"},
    ]
}


async def test_factcheck_tools_parsing_and_request() -> None:
    source, seen = factcheck(lambda r: httpx.Response(200, json=REVIEW_JSON))
    out = await source.lookup(Q)
    assert len(out) == 1
    ev = out[0]
    assert ev.text == "Le chômage a baissé de 50 % — AFP Factuel: Faux (2026-01-15)"
    assert ev.url == "https://factuel.afp.com/doc.1"
    assert ev.title == "Non, le chômage n'a pas baissé de 50 %"
    assert ev.publisher == "AFP Factuel"
    assert ev.published_at == datetime(2026, 1, 15, 10, tzinfo=UTC)
    assert ev.extra["claimant"] == "Un ministre"
    req = seen[0]
    assert req.url.path == "/v1alpha1/claims:search"
    assert req.url.params["languageCode"] == "fr"
    assert req.url.params["query"] == "le chômage a baissé"
    assert req.headers["user-agent"] == UA


async def test_factcheck_tools_empty_and_not_configured() -> None:
    source, _ = factcheck(lambda r: httpx.Response(200, json={}))
    assert await source.lookup(Q) == []
    missing = FactCheckToolsSource(api_key=None, user_agent=UA, timeout=1.0)
    with pytest.raises(SourceUnavailable) as err:
        await missing.lookup(Q)
    assert err.value.code is ec.ATL_NOT_CONFIGURED


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(401), ec.ATL_AUTH_FAILED),
        (httpx.Response(403), ec.ATL_AUTH_FAILED),
        (httpx.Response(500), ec.ATL_SOURCE_UNAVAILABLE),
        (httpx.Response(503), ec.ATL_SOURCE_UNAVAILABLE),
        (httpx.Response(429, headers={"retry-after": "60"}), ec.ATL_RATE_LIMITED),
        (httpx.Response(429), ec.ATL_RATE_LIMITED),
        (httpx.Response(200, text="<html>not json"), ec.ATL_BAD_RESPONSE),
        (httpx.Response(200, json=[1, 2]), ec.ATL_BAD_RESPONSE),
        (httpx.Response(200, json={"claims": "x"}), ec.ATL_BAD_RESPONSE),
    ],
)
async def test_error_mapping(response: httpx.Response, code: ec.ErrorCode) -> None:
    source, _ = factcheck(lambda r: response)
    with pytest.raises(SourceUnavailable) as err:
        await source.lookup(Q)
    assert err.value.code is code
    assert "SECRET-KEY" not in str(err.value)


async def test_transport_errors_are_unavailable() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    source, _ = factcheck(boom)
    with pytest.raises(SourceUnavailable) as err:
        await source.lookup(Q)
    assert err.value.code is ec.ATL_SOURCE_UNAVAILABLE


async def test_retry_after_short_sleeps_once_and_retries() -> None:
    sleeps: list[float] = []
    calls = 0

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"retry-after": "2"})
        return httpx.Response(200, json=REVIEW_JSON)

    transport, _ = counting(handler)
    source = FactCheckToolsSource(
        api_key="k", user_agent=UA, timeout=1.0, transport=transport, sleep=sleep
    )
    assert len(await source.lookup(Q)) == 1
    assert sleeps == [2.0]
    assert calls == 2

    # A second 429 after the retry is not retried again.
    calls = -10
    always = FactCheckToolsSource(
        api_key="k",
        user_agent=UA,
        timeout=1.0,
        transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={"retry-after": "1"})),
        sleep=sleep,
    )
    with pytest.raises(SourceUnavailable) as err:
        await always.lookup(Q)
    assert err.value.code is ec.ATL_RATE_LIMITED


async def test_cache_hit_avoids_http_call_fake_and_db(db: Database) -> None:
    for cache in (DictCache(), DbSourceCache(db)):
        source, seen = factcheck(lambda r: httpx.Response(200, json=REVIEW_JSON), cache=cache)
        first = await source.lookup(Q)
        second = await source.lookup(Q)
        assert len(seen) == 1
        assert [e.url for e in first] == [e.url for e in second]
        assert "SECRET-KEY" not in json.dumps(
            await cache.get(
                "factcheck_tools", {"query": Q.text, "languageCode": "fr", "pageSize": 5}
            )
        )


# --- Wikipedia ---------------------------------------------------------------------------

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
ARTICLE_HTML = (
    '<div class="mw-parser-output"><p>La loi a été adoptée en 2024.</p>'
    '<sup class="reference">[1]</sup><div class="reflist"><ol><li>noise</li></ol></div></div>'
)
EXTLINKS = [
    "https://www.insee.fr/fr/statistiques/1",
    "//www.legifrance.gouv.fr/loda/id/JORFTEXT1",
    "https://fr.wikipedia.org/wiki/X",
    "https://web.archive.org/web/2020/https://a.example",
    "https://commons.wikimedia.org/x",
    "mailto:a@b.c",
    "https://www.insee.fr/fr/statistiques/1",
]


def wiki_handler(
    *,
    timestamp: str = "2026-10-01T00:00:00Z",
    risk: float | None = 0.1,
    risk_status: int = 200,
    empty_langs: frozenset[str] = frozenset(),
) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        lang = url.host.split(".")[0]
        if url.path.endswith("/v1/search/page"):
            if lang in empty_langs:
                return httpx.Response(200, json={"pages": []})
            return httpx.Response(
                200, json={"pages": [{"id": 1, "key": "Loi_X", "title": "Loi X", "excerpt": "<b>"}]}
            )
        if url.host == "api.wikimedia.org":
            assert json.loads(request.content) == {"rev_id": 500, "lang": lang} or json.loads(
                request.content
            )["rev_id"] in (500, 499)
            if risk_status != 200:
                return httpx.Response(risk_status)
            assert risk is not None
            return httpx.Response(
                200,
                json={
                    "output": {
                        "prediction": risk >= 0.5,
                        "probabilities": {"true": risk, "false": 1 - risk},
                    }
                },
            )
        params = url.params
        if params.get("action") == "query":
            return httpx.Response(
                200,
                json={
                    "query": {
                        "pages": [
                            {
                                "pageid": 1,
                                "title": "Loi X",
                                "revisions": [
                                    {"revid": 500, "parentid": 499, "timestamp": timestamp}
                                ],
                            }
                        ]
                    }
                },
            )
        assert params["action"] == "parse"
        revid = int(params["oldid"])
        return httpx.Response(
            200,
            json={
                "parse": {
                    "title": "Loi X",
                    "revid": revid,
                    "text": ARTICLE_HTML,
                    "externallinks": EXTLINKS,
                }
            },
        )

    return handler


def wikipedia(handler: Handler, **kw: Any) -> tuple[WikipediaSource, list[httpx.Request]]:
    transport, seen = counting(handler)
    src = WikipediaSource(
        user_agent=UA, timeout=2.0, transport=transport, clock=lambda: NOW, sleep=no_sleep, **kw
    )
    return src, seen


def test_reference_urls_filters_projects_and_archives() -> None:
    assert reference_urls(EXTLINKS) == (
        "https://www.insee.fr/fr/statistiques/1",
        "https://www.legifrance.gouv.fr/loda/id/JORFTEXT1",
    )


async def test_wikipedia_fixed_revision_and_references() -> None:
    src, seen = wikipedia(wiki_handler())
    out = await src.lookup(SourceQuery(text="loi X", lang="fr", limit=1))
    assert len(out) == 1
    ev = out[0]
    assert ev.stable_id == "500"
    assert ev.url == "https://fr.wikipedia.org/w/index.php?oldid=500"
    assert ev.text == "La loi a été adoptée en 2024."
    assert ev.tier is Tier.TERTIARY
    assert ev.license == "CC BY-SA 4.0"
    assert ev.published_at == datetime(2026, 10, 1, tzinfo=UTC)
    assert ev.references == (
        "https://www.insee.fr/fr/statistiques/1",
        "https://www.legifrance.gouv.fr/loda/id/JORFTEXT1",
    )
    assert not any(r.url.host == "api.wikimedia.org" for r in seen)  # old revision: no Lift Wing
    assert all(r.headers["user-agent"] == UA for r in seen)
    parse = next(r for r in seen if r.url.params.get("action") == "parse")
    assert parse.url.params["oldid"] == "500"


async def test_wikipedia_recent_low_risk_keeps_revision() -> None:
    src, _ = wikipedia(wiki_handler(timestamp="2026-11-11T10:00:00Z", risk=0.1))
    (ev,) = await src.lookup(SourceQuery(text="loi X", lang="fr", limit=1))
    assert ev.stable_id == "500"
    assert ev.extra["revert_risk"] == "low"


async def test_wikipedia_recent_high_risk_uses_previous_revision() -> None:
    src, _ = wikipedia(wiki_handler(timestamp="2026-11-11T10:00:00Z", risk=0.9))
    (ev,) = await src.lookup(SourceQuery(text="loi X", lang="fr", limit=1))
    assert ev.stable_id == "499"
    assert ev.url.endswith("oldid=499")
    assert ev.extra["revert_risk"] == "high_previous_revision_used"


async def test_wikipedia_revert_risk_failure_marks_unknown() -> None:
    src, _ = wikipedia(wiki_handler(timestamp="2026-11-11T10:00:00Z", risk_status=500))
    (ev,) = await src.lookup(SourceQuery(text="loi X", lang="fr", limit=1))
    assert ev.stable_id == "500"
    assert ev.extra["revert_risk"] == "unknown"


async def test_wikipedia_language_fallback_order() -> None:
    src, seen = wikipedia(wiki_handler(empty_langs=frozenset({"de", "en"})))
    (ev,) = await src.lookup(SourceQuery(text="Gesetz", lang="de", limit=1))
    assert ev.lang == "fr"
    searched = [r.url.host.split(".")[0] for r in seen if r.url.path.endswith("/search/page")]
    assert searched == ["de", "en", "fr"]


async def test_wikipedia_missing_page_and_bad_response() -> None:
    def missing(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search/page"):
            return httpx.Response(200, json={"pages": [{"key": "X", "title": "X"}]})
        return httpx.Response(200, json={"query": {"pages": [{"title": "X", "missing": True}]}})

    src, _ = wikipedia(missing)
    assert await src.lookup(SourceQuery(text="x", lang="en", limit=1)) == []
    broken, _ = wikipedia(lambda r: httpx.Response(200, json={"unexpected": 1}))
    with pytest.raises(SourceUnavailable) as err:
        await broken.lookup(SourceQuery(text="x", lang="en", limit=1))
    assert err.value.code is ec.ATL_BAD_RESPONSE


async def test_wikipedia_cache_ttls_and_hit(db: Database) -> None:
    fake = DictCache()
    src, seen = wikipedia(wiki_handler(), cache=fake)
    query = SourceQuery(text="loi X", lang="fr", limit=1)
    await src.lookup(query)
    calls = len(seen)
    await src.lookup(query)
    assert len(seen) == calls
    assert set(fake.ttls) == {86_400, 30 * 86_400}
    real, seen2 = wikipedia(wiki_handler(), cache=DbSourceCache(db))
    await real.lookup(query)
    n = len(seen2)
    await real.lookup(query)
    assert len(seen2) == n


async def test_wikipedia_concurrency_never_exceeds_limit() -> None:
    import asyncio

    active = 0
    peak = 0

    class SlowTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return httpx.Response(200, json={"pages": []})

    src = WikipediaSource(user_agent=UA, timeout=2.0, transport=SlowTransport(), concurrency=3)
    await asyncio.gather(
        *(src.lookup(SourceQuery(text=f"t{i}", lang="fr", limit=1)) for i in range(12))
    )
    assert 1 <= peak <= 3


# --- Wikidata ----------------------------------------------------------------------------


def snak(value: Any, vtype: str = "string", prop: str = "P1") -> dict[str, Any]:
    return {"snaktype": "value", "property": prop, "datavalue": {"value": value, "type": vtype}}


def entity_snak(qid: str) -> dict[str, Any]:
    return snak({"entity-type": "item", "id": qid}, "wikibase-entityid")


def time_snak(date: str, precision: int = 11) -> dict[str, Any]:
    return snak({"time": date, "precision": precision}, "time")


MACRON = {
    "entities": {
        "Q3052772": {
            "id": "Q3052772",
            "lastrevid": 123,
            "labels": {"fr": {"language": "fr", "value": "Emmanuel Macron"}},
            "claims": {
                "P39": [
                    {
                        "id": "Q3052772$A",
                        "rank": "preferred",
                        "mainsnak": entity_snak("Q191954"),
                        "qualifiers": {
                            "P580": [time_snak("+2017-05-14T00:00:00Z")],
                            "P1545": [snak("25")],
                        },
                        "references": [
                            {
                                "snaks": {
                                    "P854": [snak("https://www.elysee.fr/president", prop="P854")]
                                }
                            }
                        ],
                    },
                    {
                        "id": "Q3052772$B",
                        "rank": "normal",
                        "mainsnak": entity_snak("Q3248444"),
                        "references": [{"snaks": {"P143": [entity_snak("Q8447")]}}],
                    },
                    {"id": "Q3052772$C", "rank": "deprecated", "mainsnak": entity_snak("Q1")},
                ],
                "P569": [
                    {
                        "id": "Q3052772$D",
                        "rank": "normal",
                        "mainsnak": time_snak("+1977-12-21T00:00:00Z"),
                    }
                ],
                "P1082": [
                    {
                        "id": "Q3052772$E",
                        "rank": "normal",
                        "mainsnak": snak({"amount": "+68000000", "unit": "1"}, "quantity"),
                        "references": [{"snaks": {"P248": [entity_snak("Q123")]}}],
                    }
                ],
                "P9999": [{"mainsnak": snak("ignored")}],
            },
        }
    }
}
LABELS = {
    "entities": {
        "Q191954": {
            "id": "Q191954",
            "labels": {"fr": {"language": "fr", "value": "président de la République française"}},
        },
        "Q3248444": {
            "id": "Q3248444",
            "labels": {"en": {"language": "en", "value": "co-prince of Andorra"}},
        },
        "Q8447": {"id": "Q8447", "labels": {"en": {"language": "en", "value": "French Wikipedia"}}},
        "Q123": {"id": "Q123", "labels": {"en": {"language": "en", "value": "INSEE"}}},
    }
}


def wikidata_handler(request: httpx.Request) -> httpx.Response:
    action = request.url.params["action"]
    if action == "wbsearchentities":
        return httpx.Response(
            200, json={"search": [{"id": "Q3052772", "label": "Emmanuel Macron"}]}
        )
    assert action == "wbgetentities"
    if "claims" in request.url.params["props"]:
        return httpx.Response(200, json=MACRON)
    return httpx.Response(200, json=LABELS)


async def test_wikidata_statements_tiers_and_text() -> None:
    transport, seen = counting(wikidata_handler)
    src = WikidataSource(user_agent=UA, timeout=2.0, transport=transport)
    out = await src.lookup(SourceQuery(text="Emmanuel Macron", lang="fr"))
    by_id = {e.stable_id: e for e in out}
    assert set(by_id) == {
        "Q3052772$A",
        "Q3052772$B",
        "Q3052772$D",
        "Q3052772$E",
    }  # deprecated dropped
    a = by_id["Q3052772$A"]
    assert a.tier is Tier.SECONDARY
    assert a.references == ("https://www.elysee.fr/president",)
    assert a.text == (
        "Emmanuel Macron — position held: président de la République française "
        "(qualifiers: start time 2017-05-14; series ordinal 25); "
        "reference: https://www.elysee.fr/president"
    )
    assert by_id["Q3052772$B"].tier is Tier.TERTIARY  # "imported from" is not a reference
    assert by_id["Q3052772$B"].references == ()
    assert by_id["Q3052772$D"].tier is Tier.TERTIARY
    assert "date of birth: 1977-12-21" in by_id["Q3052772$D"].text
    pop = by_id["Q3052772$E"]
    assert pop.tier is Tier.SECONDARY and "population: 68000000" in pop.text
    assert "stated in: INSEE" in pop.text
    assert a.license == "CC0 1.0"
    assert a.version == "123"
    # search + entity + one label call
    assert [r.url.params["action"] for r in seen] == [
        "wbsearchentities",
        "wbgetentities",
        "wbgetentities",
    ]


async def test_wikidata_subject_qid_skips_search_and_caches(db: Database) -> None:
    transport, seen = counting(wikidata_handler)
    src = WikidataSource(user_agent=UA, timeout=2.0, transport=transport, cache=DbSourceCache(db))
    query = SourceQuery(text="ignored", lang="fr", subject_qid="Q3052772")
    await src.lookup(query)
    assert all(r.url.params["action"] == "wbgetentities" for r in seen)
    n = len(seen)
    await src.lookup(query)
    assert len(seen) == n


async def test_wikidata_no_result_and_errors() -> None:
    empty = WikidataSource(
        user_agent=UA,
        timeout=1.0,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"search": []})),
    )
    assert await empty.lookup(SourceQuery(text="zzz", lang="en")) == []
    bad = WikidataSource(
        user_agent=UA,
        timeout=1.0,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"nope": 1})),
    )
    with pytest.raises(SourceUnavailable) as err:
        await bad.lookup(SourceQuery(text="zzz", lang="en"))
    assert err.value.code is ec.ATL_BAD_RESPONSE


# --- Brave -------------------------------------------------------------------------------

BRAVE_JSON = {
    "web": {
        "results": [
            {
                "title": "INSEE",
                "url": "https://www.insee.fr/fr/a",
                "description": "d1",
                "page_age": "2026-05-01T00:00:00",
            },
            {"title": "Blog", "url": "https://blog.example.com/p", "description": "d2"},
            {
                "title": "Min",
                "url": "https://sante.gouv.fr/x",
                "description": "d3",
                "language": "fr",
            },
        ]
    }
}


def brave(handler: Handler, **kw: Any) -> tuple[BraveSearch, list[httpx.Request]]:
    transport, seen = counting(handler)
    return (
        BraveSearch(
            api_key="BRAVE-SECRET",
            user_agent=UA,
            timeout=2.0,
            transport=transport,
            sleep=no_sleep,
            **kw,
        ),
        seen,
    )


def test_brave_build_query_site_groups() -> None:
    assert (
        BraveSearch.build_query("q", ["insee.fr", "gouv.fr"], [])
        == "q (site:insee.fr OR site:gouv.fr)"
    )
    assert BraveSearch.build_query("q", ["insee.fr"], ["x.com"]) == "q site:insee.fr -site:x.com"
    assert BraveSearch.build_query("q", ["bad domain!"], []) == "q"
    many = BraveSearch.build_query("q", [f"d{i}.fr" for i in range(30)], [])
    assert many.count("site:") == 10


async def test_brave_domain_restriction_then_broadening() -> None:
    src, seen = brave(lambda r: httpx.Response(200, json=BRAVE_JSON))
    restricted = await src.search(
        "budget", limit=5, lang="fr", country="FR", include_domains=["insee.fr", "gouv.fr"]
    )
    assert [r.url for r in restricted] == ["https://www.insee.fr/fr/a", "https://sante.gouv.fr/x"]
    assert restricted[0].published_at == datetime(2026, 5, 1, tzinfo=UTC)
    assert restricted[1].lang == "fr"
    req = seen[0]
    assert req.headers["x-subscription-token"] == "BRAVE-SECRET"
    assert req.url.params["q"] == "budget (site:insee.fr OR site:gouv.fr)"
    assert req.url.params["country"] == "FR" and req.url.params["search_lang"] == "fr"
    broad = await src.search("budget", limit=5)
    assert len(broad) == 3
    assert seen[1].url.params["q"] == "budget"


async def test_brave_cache_errors_and_not_configured(db: Database) -> None:
    src, seen = brave(lambda r: httpx.Response(200, json=BRAVE_JSON), cache=DbSourceCache(db))
    await src.search("q", limit=3)
    await src.search("q", limit=3)
    assert len(seen) == 1
    limited, _ = brave(lambda r: httpx.Response(429, headers={"retry-after": "30"}))
    with pytest.raises(SourceUnavailable) as err:
        await limited.search("q", limit=3)
    assert err.value.code is ec.ATL_RATE_LIMITED
    empty, _ = brave(lambda r: httpx.Response(200, json={"query": {}}))
    assert await empty.search("q", limit=3) == []
    with pytest.raises(SourceUnavailable) as nc:
        await BraveSearch(api_key=None, user_agent=UA, timeout=1.0).search("q", limit=1)
    assert nc.value.code is ec.ATL_NOT_CONFIGURED


# --- Légifrance --------------------------------------------------------------------------

ART_ID = "LEGIARTI000006417749"


class Piste:
    def __init__(self, *, expires_in: int = 3600, article_401_once: bool = False) -> None:
        self.tokens = 0
        self.expires_in = expires_in
        self.article_401_once = article_401_once
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/oauth/token"):
            self.tokens += 1
            body = dict(p.split("=", 1) for p in request.content.decode().split("&"))
            assert body["grant_type"] == "client_credentials" and body["scope"] == "openid"
            return httpx.Response(
                200, json={"access_token": f"tok{self.tokens}", "expires_in": self.expires_in}
            )
        assert request.headers["authorization"] == f"Bearer tok{self.tokens}"
        if self.article_401_once and self.tokens == 1:
            return httpx.Response(401)
        if path.endswith("/search"):
            if json.loads(request.content)["fond"] == "LODA_DATE":
                return httpx.Response(200, json={"results": []})
            return httpx.Response(
                200, json={"results": [{"titles": [{"id": ART_ID, "title": "Article 221-1"}]}]}
            )
        assert path.endswith("/consult/getArticle")
        return httpx.Response(
            200,
            json={
                "article": {
                    "id": ART_ID,
                    "num": "221-1",
                    "etat": "VIGUEUR",
                    "texte": "Le meurtre est le fait de donner la mort à autrui.<br/>Il est puni.",
                    "dateDebut": 1_000_000_000_000,
                    "dateFin": 32_472_144_000_000,
                    "context": {"titreTxt": [{"titre": "Code pénal"}]},
                }
            },
        )


def legifrance(piste: Piste, **kw: Any) -> LegifranceSource:
    return LegifranceSource(
        client_id="cid",
        client_secret="CSECRET",
        user_agent=UA,
        timeout=2.0,
        transport=httpx.MockTransport(piste),
        sleep=no_sleep,
        **kw,
    )


async def test_legifrance_article_dates_and_token_reuse() -> None:
    piste = Piste()
    src = legifrance(piste)
    out = await src.lookup(
        SourceQuery(text="meurtre", lang="fr", limit=1, as_of=datetime(2010, 1, 1, tzinfo=UTC))
    )
    await src.lookup(SourceQuery(text="meurtre", lang="fr", limit=1))
    assert piste.tokens == 1
    (ev,) = out
    assert ev.stable_id == ART_ID
    assert ev.tier is Tier.PRIMARY
    assert ev.license == "Licence Ouverte 2.0"
    assert ev.url == f"https://www.legifrance.gouv.fr/codes/article_lc/{ART_ID}"
    assert ev.published_at == datetime(2001, 9, 9, 1, 46, 40, tzinfo=UTC)
    assert ev.text.startswith(
        "Code pénal — Article 221-1 (in force from 2001-09-09 to open end): Le meurtre"
    )
    assert "Il est puni." in ev.text
    assert ev.extra["as_of_match"] == "true"
    first_search = next(r for r in piste.requests if r.url.path.endswith("/search"))
    body = json.loads(first_search.content)
    assert body["recherche"]["filtres"][0]["facette"] == "DATE_VERSION"
    assert str(first_search.url).startswith(
        "https://api.piste.gouv.fr/dila/legifrance/lf-engine-app/"
    )


async def test_legifrance_as_of_before_validity_flagged() -> None:
    src = legifrance(Piste())
    (ev,) = await src.lookup(
        SourceQuery(text="meurtre", lang="fr", limit=1, as_of=datetime(1990, 1, 1, tzinfo=UTC))
    )
    assert ev.extra["as_of_match"] == "false"


async def test_legifrance_token_refresh_on_expiry_and_on_401() -> None:
    clock = [0.0]
    piste = Piste(expires_in=100)
    src = legifrance(piste, monotonic=lambda: clock[0])
    q = SourceQuery(text="meurtre", lang="fr", limit=1)
    await src.lookup(q)
    clock[0] = 30.0
    await src.lookup(q)
    assert piste.tokens == 1
    clock[0] = 500.0
    await src.lookup(q)
    assert piste.tokens == 2
    refreshed = Piste(article_401_once=True)
    assert len(await legifrance(refreshed).lookup(q)) == 1
    assert refreshed.tokens == 2


async def test_legifrance_auth_failures_and_sandbox() -> None:
    denied = LegifranceSource(
        client_id="cid",
        client_secret="CSECRET",
        user_agent=UA,
        timeout=1.0,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(401, json={"error": "invalid_client"})
        ),
    )
    with pytest.raises(SourceUnavailable) as err:
        await denied.lookup(SourceQuery(text="x", lang="fr"))
    assert err.value.code is ec.ATL_AUTH_FAILED
    assert "CSECRET" not in str(err.value)
    piste = Piste()
    sandbox = legifrance(piste, sandbox=True)
    await sandbox.lookup(SourceQuery(text="meurtre", lang="fr", limit=1))
    assert piste.requests[0].url.host == "sandbox-oauth.piste.gouv.fr"
    assert piste.requests[1].url.host == "sandbox-api.piste.gouv.fr"


# --- INSEE -------------------------------------------------------------------------------

SDMX = """<?xml version='1.0' encoding='UTF-8'?>
<message:StructureSpecificData xmlns:message="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message"
 xmlns:ss="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/data/structurespecific">
<message:DataSet><Series IDBANK="001688370" FREQ="T"
 TITLE_FR="Taux de chômage au sens du BIT - France" LAST_UPDATE="2026-08-14"
 UNIT_MEASURE="POURCENT" UNIT_MULT="0">
<Obs TIME_PERIOD="2026-Q2" OBS_VALUE="7.5" OBS_STATUS="P"/>
<Obs TIME_PERIOD="2026-Q1" OBS_VALUE="7.4" OBS_STATUS="A"/>
<Obs TIME_PERIOD="2025-Q4" OBS_VALUE="7.3" OBS_STATUS="A"/>
<Obs TIME_PERIOD="2025-Q3" OBS_VALUE="7.6" OBS_STATUS="A"/>
</Series></message:DataSet></message:StructureSpecificData>"""


def insee(handler: Handler, **kw: Any) -> tuple[InseeSource, list[httpx.Request]]:
    transport, seen = counting(handler)
    return (
        InseeSource(
            series={"taux de chômage": "001688370", "inflation": "001759970"},
            user_agent=UA,
            timeout=2.0,
            transport=transport,
            **kw,
        ),
        seen,
    )


def test_insee_series_config_and_matching() -> None:
    assert parse_series_config('{"a": "123456789", "b": "bad"}') == {"a": "123456789"}
    assert (
        parse_series_config("") == {}
        and parse_series_config("[1]") == {}
        and parse_series_config("{") == {}
    )
    src, _ = insee(lambda r: httpx.Response(200, text=SDMX))
    assert src.matching("Le TAUX de chomage a augmenté") == ["001688370"]
    assert src.matching("le taux de croissance") == []
    assert src.matching("inflation et chômage") == ["001759970"]


async def test_insee_latest_observations(db: Database) -> None:
    src, seen = insee(lambda r: httpx.Response(200, text=SDMX), cache=DbSourceCache(db))
    query = SourceQuery(
        text="le taux de chômage", lang="fr", as_of=datetime(2020, 1, 1, tzinfo=UTC)
    )
    (ev,) = await src.lookup(query)
    assert ev.stable_id == "001688370"
    assert ev.url == "https://www.insee.fr/fr/statistiques/serie/001688370"
    assert ev.text.splitlines()[0] == (
        "Taux de chômage au sens du BIT - France: 7.5 POURCENT (2026-Q2), published 2026-08-14"
    )
    assert len(ev.text.splitlines()) == 4
    assert ev.tier is Tier.PRIMARY and ev.license == "Licence Ouverte 2.0"
    assert ev.published_at == datetime(2026, 8, 14, tzinfo=UTC)
    assert ev.extra["as_of"].startswith("ignored")
    assert seen[0].url.path == "/series/sdmx/data/SERIES_BDM/001688370"
    assert seen[0].url.params["lastNObservations"] == "4"
    await src.lookup(query)
    assert len(seen) == 1


async def test_insee_errors() -> None:
    q = SourceQuery(text="taux de chômage", lang="fr")
    for response, code in (
        (httpx.Response(200, text="not xml <"), ec.ATL_BAD_RESPONSE),
        (httpx.Response(500), ec.ATL_SOURCE_UNAVAILABLE),
        (httpx.Response(404), ec.ATL_BAD_RESPONSE),
    ):
        src, _ = insee(lambda r, resp=response: resp)  # type: ignore[misc]
        with pytest.raises(SourceUnavailable) as err:
            await src.lookup(q)
        assert err.value.code is code
    empty, _ = insee(lambda r: httpx.Response(200, text="<root/>"))
    assert await empty.lookup(q) == []


# --- import isolation --------------------------------------------------------------------


def test_atlas_modules_do_not_import_httpx() -> None:
    code = (
        "import sys\n"
        "import tower.core.adapters.knowledge.atlas, tower.core.adapters.knowledge.factory\n"
        "import tower.core.adapters.knowledge.wikipedia, tower.core.adapters.knowledge.wikidata\n"
        "import tower.core.adapters.knowledge.brave, tower.core.adapters.knowledge.legifrance\n"
        "import tower.core.adapters.knowledge.insee\n"
        "import tower.core.adapters.knowledge.factcheck_tools\n"
        "print('httpx' in sys.modules)\n"
    )
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    )
    assert out.stdout.strip() == "False"
