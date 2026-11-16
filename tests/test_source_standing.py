"""Standing of sources (no database): closed rules, Atlas rating, Wikidata official sites
(fake HTTP transport), first-party rule in the independence assessment, display."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tower.core.adapters.knowledge.atlas import Atlas, RegistryInfo
from tower.core.adapters.knowledge.http import SourceHttp
from tower.core.adapters.knowledge.wikidata_sites import (
    IDENTITY_CLASSES,
    make_subject_sites,
    parse_identity_line,
    site_host,
    stream_identities,
)
from tower.core.domain.factcheck.standing import (
    IDENTITY_KINDS,
    IDENTITY_RANK,
    JudgedStanding,
    Standing,
    effective_tier,
    first_party_is_stakeholder,
    host_matches,
    host_suffixes,
    identity_standing,
    is_shared_host,
    judged_tier,
    registry_standing,
    suffix_standing,
)
from tower.core.domain.factcheck.values import ClaimType, Veracity
from tower.core.factcheck.schemas import PassageOrigin
from tower.core.factcheck.standings import first_party_urls, url_host
from tower.services.atlas.identities import kind_of_class
from tower.services.investigate import pipeline as pl

from .test_investigate_pipeline import _cite, _judge, _pd, make_view

# ----------------------------------------------------------------- closed rules


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("www.legifrance.gouv.fr", ("primary", Standing.OFFICIAL)),
        ("data.gov.uk", ("primary", Standing.OFFICIAL)),
        ("nasa.gov", ("primary", Standing.OFFICIAL)),
        ("ec.europa.eu", ("primary", Standing.OFFICIAL)),
        ("who.int", ("primary", Standing.OFFICIAL)),
        ("www.mlit.go.jp", ("primary", Standing.OFFICIAL)),
        ("www.bundesregierung.bund.de", ("primary", Standing.OFFICIAL)),
        ("www.argentina.gob.ar", ("primary", Standing.OFFICIAL)),
        ("math.mit.edu", ("secondary", Standing.ACADEMIC)),
        ("www.ox.ac.uk", ("secondary", Standing.ACADEMIC)),
        ("www.usp.edu.br", ("secondary", Standing.ACADEMIC)),
        ("academia.edu", None),  # social network, not a university
        ("gov.uk", None),  # a bare suffix is not a site
        ("gouv.fr", None),
        ("example.com", None),
        ("go.com", None),  # "go" + a non-country label
        ("ministry.gov.facebook.com", None),
    ],
)
def test_suffix_standing(host: str, expected: tuple[str, Standing] | None) -> None:
    assert suffix_standing(host) == expected


def test_host_helpers() -> None:
    assert host_suffixes("www.a.b.example.org") == [
        "a.b.example.org",
        "b.example.org",
        "example.org",
    ]
    assert host_suffixes("localhost") == ["localhost"]
    assert is_shared_host("someone.github.io") and is_shared_host("www.facebook.com")
    assert not is_shared_host("anthropic.com")
    assert host_matches("docs.anthropic.com", ["anthropic.com"])
    assert host_matches("anthropic.com", ["www.anthropic.com"])
    assert not host_matches("notanthropic.com", ["anthropic.com"])
    assert not host_matches("facebook.com", ["facebook.com"])  # platform: never first-party
    assert url_host("https://WWW.Anthropic.com/news?x=1") == "anthropic.com"
    urls = ["https://docs.anthropic.com/a", "https://lemonde.fr/b"]
    assert first_party_urls(urls, ("anthropic.com",)) == {"https://docs.anthropic.com/a"}
    assert first_party_urls(urls, ()) == set()


def test_effective_tier_rules() -> None:
    fp = Standing.FIRST_PARTY.value
    assert effective_tier("unrated", fp) == "primary"
    assert effective_tier("tertiary", fp) == "primary"
    assert effective_tier("avoid", fp) == "avoid"  # the registry's "avoid" always wins
    assert effective_tier("unrated", "official") == "secondary"  # capped: never primary
    assert effective_tier("unrated", "reference") == "secondary"
    assert effective_tier("unrated", "user_generated") == "tertiary"
    assert effective_tier("unrated", "press") == "unrated"
    assert effective_tier("unrated", None) == "unrated"
    assert effective_tier("tertiary", "official") == "tertiary"  # only unrated is refined
    for judged in JudgedStanding:
        assert judged_tier("unrated", judged.value) != "primary"


def test_first_party_stakeholder_and_maps() -> None:
    assert first_party_is_stakeholder(ClaimType.CONDUCT.value)
    assert first_party_is_stakeholder(ClaimType.ACCUSATION.value)
    assert not first_party_is_stakeholder(ClaimType.STATISTIC.value)
    assert not first_party_is_stakeholder("nonsense")
    assert identity_standing("official") == ("primary", Standing.OFFICIAL)
    assert identity_standing("press") == ("unrated", Standing.PRESS)
    assert identity_standing("nope") is None
    assert registry_standing("statistics") is Standing.OFFICIAL
    assert registry_standing("other") is None and registry_standing(None) is None
    assert set(IDENTITY_RANK) == set(IDENTITY_KINDS) == set(IDENTITY_CLASSES)
    assert kind_of_class("Q11032") == "press" and kind_of_class("Q1") is None
    every = [c for items in IDENTITY_CLASSES.values() for c in items]
    assert len(every) == len(set(every))


# ------------------------------------------------------------------- Atlas rating


def test_atlas_rate_registry_then_suffix_then_unrated() -> None:
    registry = {
        "insee.fr": RegistryInfo("insee.fr", "Insee", "primary", "statistics", "FR"),
        "fake.gouv.fr": RegistryInfo("fake.gouv.fr", "Fake", "avoid", "other", "FR"),
    }
    atlas = Atlas(sources={}, search=None, fetcher=None, cache=None, registry=registry)
    r = atlas.rate("https://www.insee.fr/fr/statistiques")
    assert (r.tier, r.standing, r.basis, r.publisher) == (
        "primary",
        "official",
        "registry",
        "Insee",
    )
    r = atlas.rate("https://fake.gouv.fr/x")  # the registry beats the suffix rule
    assert (r.tier, r.basis) == ("avoid", "registry")
    r = atlas.rate("https://www.economie.gouv.fr/x")
    assert (r.tier, r.standing, r.basis) == ("primary", "official", "suffix")
    r = atlas.rate("https://mathworld.example/prime")
    assert (r.tier, r.standing, r.basis) == ("unrated", None, "default")
    assert atlas.tier_of("https://mathworld.example/prime") == ("unrated", None)


# --------------------------------------------------------------- Wikidata sites


def test_parse_identity_line_and_site_host() -> None:
    assert parse_identity_line("?item\t?site\t?iso") is None
    row = parse_identity_line(
        '<http://www.wikidata.org/entity/Q752075>\t<https://www.aanda.org/>\t"FR"'
    )
    assert row is not None and (row.domain, row.qid, row.country) == ("aanda.org", "Q752075", "FR")
    row = parse_identity_line('<http://www.wikidata.org/entity/Q1>\t<https://x.org/>\t"SUN"')
    assert row is not None and row.country is None
    assert (
        parse_identity_line("<http://www.wikidata.org/entity/Q1>\t<https://facebook.com/x>") is None
    )
    assert parse_identity_line("<http://www.wikidata.org/entity/P1>\t<https://x.org/>") is None
    assert site_host("ftp://x.org") is None and site_host("https://localhost/") is None
    assert site_host("https://WWW.Insee.FR/fr") == "insee.fr"


def _claim(prop: str, value: Any, *, ended: bool = False, rank: str = "normal") -> dict[str, Any]:
    return {
        "rank": rank,
        "mainsnak": {"snaktype": "value", "datavalue": {"value": value}},
        "qualifiers": {"P582": [{}]} if ended else {},
    }


async def test_subject_sites_follow_the_developer() -> None:
    entities: dict[str, Any] = {
        "Q100": {  # a product without its own site, developed by Q200
            "claims": {
                "P178": [_claim("P178", {"id": "Q200"})],
                "P127": [_claim("P127", {"id": "Q300"}, ended=True)],  # no longer the owner
                "P123": [_claim("P123", {"id": "Q400"}, rank="deprecated")],
            }
        },
        "Q200": {"claims": {"P856": [_claim("P856", "https://www.anthropic.com/")]}},
        "Q300": {"claims": {"P856": [_claim("P856", "https://old-owner.example/")]}},
    }
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ids = request.url.params["ids"].split("|")
        calls.append(request.url.params["ids"])
        return httpx.Response(
            200, json={"entities": {i: entities.get(i, {"missing": ""}) for i in ids}}
        )

    http = SourceHttp(
        "wikidata", timeout=5, user_agent="test", transport=httpx.MockTransport(handler)
    )
    try:
        fetch = make_subject_sites(http, None, 60)
        sites = await fetch("Q100")
        assert sites.hosts == ("anthropic.com",)
        assert calls == ["Q100", "Q200"]  # ended and deprecated relations are not followed
        assert (await fetch("not-a-qid")).hosts == ()
        assert (await fetch("Q999")).hosts == ()
    finally:
        await http.aclose()


async def test_stream_identities_reads_tsv() -> None:
    body = (
        "?item\t?site\t?iso\n"
        '<http://www.wikidata.org/entity/Q1>\t<https://www.lemonde.fr/>\t"FR"\n'
        "<http://www.wikidata.org/entity/Q2>\t<https://twitter.com/x>\n"
        "<http://www.wikidata.org/entity/Q3>\t<https://news.example.org/a>\n"
    )
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = request.url.params["query"]
        seen["accept"] = request.headers["accept"]
        return httpx.Response(200, text=body)

    http = SourceHttp(
        "wikidata", timeout=5, user_agent="test", transport=httpx.MockTransport(handler)
    )
    try:
        rows = [r async for r in stream_identities(http, "Q11032")]
    finally:
        await http.aclose()
    assert [(r.domain, r.qid, r.country) for r in rows] == [
        ("lemonde.fr", "Q1", "FR"),
        ("news.example.org", "Q3", None),
    ]
    assert "wd:Q11032" in seen["query"] and seen["accept"] == "text/tab-separated-values"
    with pytest.raises(ValueError, match="not a QID"):
        [r async for r in stream_identities(http, "Q1; DROP")]


async def test_stream_identities_maps_http_errors() -> None:
    from tower.core.ports.knowledge import SourceUnavailable

    http = SourceHttp(
        "wikidata",
        timeout=5,
        user_agent="test",
        transport=httpx.MockTransport(lambda r: httpx.Response(503)),
    )
    try:
        with pytest.raises(SourceUnavailable):
            [r async for r in stream_identities(http, "Q11032")]
    finally:
        await http.aclose()


# ------------------------------------------------- first party in the assessment


def _two_passages(claim_type: str) -> tuple[list[pl.PassageData], Any]:
    passages = [
        _pd(
            "P1",
            "Claude can read PDF files up to 100 pages.",
            source="https://docs.anthropic.com/pdf",
            domain="anthropic.com",
            tier="unrated",
            standing=Standing.FIRST_PARTY.value,
            simhash=0,
        ),
        _pd("P2", "The assistant reads PDF files, the company says.", simhash=-1),
    ]
    judge = _judge(
        [_cite("P1", "Claude can read PDF files"), _cite("P2", "The assistant reads PDF files")],
        [
            PassageOrigin(passage_id="P1", origin="Anthropic", primary=True),
            PassageOrigin(passage_id="P2", origin="Newspaper", primary=False),
        ],
    )
    return passages, judge


def test_first_party_is_primary_for_a_descriptive_claim() -> None:
    passages, judge = _two_passages(ClaimType.STATISTIC.value)
    a = pl.assess_judgement(judge, passages, claim=make_view())
    assert a.veracity is Veracity.TRUE and not a.downgraded
    assert a.independence.has_primary
    assert [r.tier for r in a.refs] == ["primary", "secondary"]


def test_first_party_is_a_stakeholder_for_a_conduct_claim() -> None:
    passages, judge = _two_passages(ClaimType.CONDUCT.value)
    a = pl.assess_judgement(
        judge, passages, claim=make_view(claim_type=ClaimType.CONDUCT.value, entities=())
    )
    assert [r.stakeholder for r in a.refs] == [True, False]
    assert a.veracity is Veracity.UNVERIFIABLE and a.downgraded


def test_unrated_source_without_first_party_cannot_be_primary() -> None:
    passages, judge = _two_passages(ClaimType.STATISTIC.value)
    passages[0] = _pd(
        "P1", passages[0].text, source=passages[0].source_url, tier="unrated", simhash=0
    )
    judge = judge.model_copy(
        update={
            "origins": [
                PassageOrigin(
                    passage_id="P1",
                    origin="Anthropic",
                    primary=True,
                    standing=JudgedStanding.OFFICIAL,
                ),
                *judge.origins[1:],
            ]
        }
    )
    a = pl.assess_judgement(judge, passages, claim=make_view())
    assert [r.tier for r in a.refs] == ["secondary", "secondary"]  # judged: capped
    assert a.downgraded  # the judge cannot create the primary source the rule requires
    assert pl.judged_standings(judge, passages) == {"P1": "official"}


def test_judged_standings_skip_known_and_first_party_sources() -> None:
    passages = [
        _pd("P1", "a", source_standing="press", tier="unrated"),
        _pd("P2", "b", standing="first_party", tier="unrated"),
        _pd("P3", "c", tier="unrated"),
    ]
    judge = _judge(
        [],
        [
            PassageOrigin(
                passage_id=p, origin="x", primary=False, standing=JudgedStanding.REFERENCE
            )
            for p in ("P1", "P2", "P3", "P9")
        ],
    )
    assert pl.judged_standings(judge, passages) == {"P3": "reference"}


def test_passage_views_show_first_party_to_the_judge() -> None:
    passages = [
        _pd("P1", "a", tier="unrated", standing="first_party"),
        _pd("P2", "b", tier="unrated", source_standing="press"),
        _pd("P3", "c", tier="unrated", standing="reference"),  # judged: not shown to models
    ]
    views = pl.passage_views(passages)
    assert (views["P1"].source.tier, views["P1"].source.standing) == ("primary", "first_party")
    assert (views["P2"].source.tier, views["P2"].source.standing) == ("unrated", "press")
    assert (views["P3"].source.tier, views["P3"].source.standing) == ("unrated", None)


def test_old_judgements_without_standing_still_load() -> None:
    raw = {"passage_id": "P1", "origin": "Insee", "primary": True}
    assert PassageOrigin.model_validate(raw).standing is None
    assert json.loads(PassageOrigin.model_validate(raw).model_dump_json())["standing"] is None


def test_forum_lists_the_standing_of_sources() -> None:
    from tower.intercom.handlers import forum

    view = {
        "rationale": [
            {
                "assertion": "x",
                "citations": [
                    {
                        "valid": True,
                        "source": {
                            "url": "https://docs.anthropic.com/a",
                            "publisher": "Anthropic",
                            "standing": "first_party",
                        },
                    },
                    {
                        "valid": True,
                        "source": {"url": "https://m.example/p", "standing": "unclear"},
                    },
                ],
            }
        ]
    }
    sources = forum._sources(view)
    assert sources["https://docs.anthropic.com/a"] == (1, "Anthropic · subject's official site")
    assert sources["https://m.example/p"] == (2, "https://m.example/p")
