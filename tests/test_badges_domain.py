"""Badge domain, Wikidata parsing and X batch lookup (no database)."""

from __future__ import annotations

import httpx
import pytest

from tower.core.adapters.wikidata_badges import (
    accounts_queries,
    details_query,
    parse_accounts,
    parse_details,
)
from tower.core.adapters.x_client import XPlatformClient
from tower.core.domain import error_codes as ec
from tower.core.domain.badges import (
    CATEGORY_COLORS,
    CATEGORY_COLORS_DARK,
    BadgeCategory,
    check_expertise,
    check_url,
    classify_wikidata,
    color_dark_of,
    color_of,
    commons_file_url,
    is_qid,
    parse_category,
    parse_lookup_ids,
    pick_category,
    stored_values,
    wikidata_expertise,
)
from tower.core.domain.errors import InvalidInput
from tower.core.domain.security import (
    GRANTABLE_PERMISSIONS,
    Actor,
    ActorKind,
    Permission,
)
from tower.core.ports.badges import WikidataAccount
from tower.core.usecases.badge_sources import batch_pairs

C = BadgeCategory


def test_colors_match_the_spec() -> None:
    assert CATEGORY_COLORS == {
        C.INSTITUTIONAL: "#F46600",
        C.ORGANIZATION: "#E30B5C",
        C.MEDIA: "#EA00FF",
        C.EXPERT: "#16A34A",
        C.VERIFIED: "#335CFF",
    }
    assert color_of("media") == "#EA00FF"
    assert color_dark_of("media") == "#EE33FF"
    assert color_dark_of("verified") == "#6D8AFF"
    assert set(CATEGORY_COLORS_DARK) == set(C)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ([C.VERIFIED, C.ORGANIZATION], C.ORGANIZATION),
        ([C.ORGANIZATION, C.EXPERT], C.EXPERT),
        ([C.EXPERT, C.MEDIA], C.MEDIA),
        ([C.MEDIA, C.INSTITUTIONAL, C.VERIFIED], C.INSTITUTIONAL),
        (["personality"], C.VERIFIED),
        ([], None),
    ],
)
def test_pick_category_priority(given: list[C | str], expected: C | None) -> None:
    assert pick_category(given) is expected


def test_parse_category_rejects_unknown() -> None:
    assert parse_category("expert") is C.EXPERT
    with pytest.raises(InvalidInput) as exc:
        parse_category("personnalite")
    assert exc.value.error_code is ec.BDG_BAD_CATEGORY


def test_personality_is_a_legacy_alias_of_verified() -> None:
    assert parse_category("personality") is C.VERIFIED
    assert C("personality") is C.VERIFIED
    assert color_of("personality") == "#335CFF"
    assert stored_values(C.VERIFIED) == ["verified", "personality"]
    assert stored_values(C.MEDIA) == ["media"]
    assert "personality" not in [c.value for c in C]


def test_check_expertise() -> None:
    assert check_expertise(C.EXPERT, "  virologie ") == "virologie"
    assert check_expertise(C.EXPERT, "") is None
    assert check_expertise(C.MEDIA, None) is None
    assert check_expertise(C.MEDIA, " ") is None
    with pytest.raises(InvalidInput) as exc:
        check_expertise(C.VERIFIED, "musique")
    assert exc.value.error_code is ec.BDG_EXPERTISE_NOT_EXPERT
    with pytest.raises(InvalidInput) as exc:
        check_expertise(C.EXPERT, "x" * 121)
    assert exc.value.error_code is ec.BDG_BAD_TEXT


def test_wikidata_expertise_prefers_precise_fields() -> None:
    assert wikidata_expertise(["Q39631"]) == "médecine"
    assert wikidata_expertise(["Q901", "Q169470", "Q170790"]) == "physique, mathématiques"
    assert wikidata_expertise(["Q901", "Q1650915"]) == "sciences, recherche"
    assert wikidata_expertise(["Q1930187"]) is None


def test_classify_human_uses_occupations_only() -> None:
    # journalist + politician → institutional wins; P31 types are ignored for humans
    assert classify_wikidata(["Q5", "Q7278"], ["Q1930187", "Q82955"]) is C.INSTITUTIONAL
    assert classify_wikidata(["Q5"], ["Q1930187", "Q901"]) is C.MEDIA
    assert classify_wikidata(["Q5"], ["Q937857"]) is C.VERIFIED
    assert classify_wikidata(["Q5"], ["Q999999999"]) is None
    assert classify_wikidata(["Q5"], []) is None


def test_classify_organisation_uses_types_only() -> None:
    assert classify_wikidata(["Q7278"], []) is C.INSTITUTIONAL
    assert classify_wikidata(["Q11032", "Q783794"], []) is C.MEDIA
    assert classify_wikidata(["Q476028"], ["Q82955"]) is C.ORGANIZATION
    assert classify_wikidata(["Q6256"], []) is None  # country: not listed


def test_parse_lookup_ids() -> None:
    plat, ids = parse_lookup_ids("x", ["12", " 34", "12", ""])
    assert plat.value == "x"
    assert ids == ["12", "34"]
    for bad in (["@jules"], ["abc"], ["1" * 20], []):
        with pytest.raises(InvalidInput):
            parse_lookup_ids("x", bad)
    with pytest.raises(InvalidInput) as exc:
        parse_lookup_ids("x", [str(i) for i in range(101)])
    assert exc.value.error_code is ec.BDG_BAD_LOOKUP
    # Other platforms: printable opaque ids (future platforms)
    assert parse_lookup_ids("bluesky", ["did:plc:abc"])[1] == ["did:plc:abc"]
    with pytest.raises(InvalidInput):
        parse_lookup_ids("myspace", ["1"])


def test_check_url() -> None:
    assert check_url(" https://a.b/c.png ", field="logo_url") == "https://a.b/c.png"
    assert check_url("", field="logo_url") is None
    for bad in ("http://a.b", "javascript:alert(1)", "https://a b"):
        with pytest.raises(InvalidInput):
            check_url(bad, field="logo_url")


def test_commons_url_and_qid() -> None:
    assert commons_file_url("Logo X.svg") == (
        "https://commons.wikimedia.org/wiki/Special:FilePath/Logo_X.svg"
    )
    assert commons_file_url("a/b.png") is None
    assert is_qid("Q142") and not is_qid("Q0") and not is_qid("P31") and not is_qid("Q")


def test_batch_pairs_skips_ambiguous_and_invalid() -> None:
    accounts = [
        WikidataAccount("Q1", "100", "one"),
        WikidataAccount("Q2", "200", None),
        WikidataAccount("Q3", "200", None),  # same X id on two items: skipped
        WikidataAccount("Q4", "h_yorozu06", None),  # not a numeric id: skipped
        WikidataAccount("Q1", "100", "one"),  # duplicate row: kept once
    ]
    triples, skipped = batch_pairs(accounts)
    assert triples == [["Q1", "100", "one"]]
    assert skipped == 2


def test_sparql_queries_and_parsing() -> None:
    queries = accounts_queries("Q142")
    assert len(queries) == 2
    assert "wdt:P27 wd:Q142" in queries[0] and "wdt:P17 wd:Q142" in queries[1]
    assert all("pq:P6552" in q and "pq:P582" in q for q in queries)
    assert "wd:Q484652" in accounts_queries("intl")[0]
    with pytest.raises(ValueError, match="invalid scope"):
        accounts_queries("Q142 } DROP")
    q = details_query(["Q1", "bad", "Q2"])
    assert "wd:Q1 wd:Q2" in q and "bad" not in q and "p:P108" in q and "pq:P582" in q

    e = "http://www.wikidata.org/entity/"
    body = {
        "results": {
            "bindings": [
                {"item": {"value": e + "Q1"}, "xid": {"value": "100"}, "handle": {"value": "a"}},
                {"item": {"value": "junk"}, "xid": {"value": "1"}},
            ]
        }
    }
    assert parse_accounts(body) == [WikidataAccount("Q1", "100", "a")]
    details = {
        "results": {
            "bindings": [
                {"item": {"value": e + "Q1"}, "k": {"value": "P31"}, "v": {"value": e + "Q5"}},
                {"item": {"value": e + "Q1"}, "k": {"value": "P106"}, "v": {"value": e + "Q901"}},
                {"item": {"value": e + "Q1"}, "k": {"value": "label"}, "v": {"value": "Marie"}},
                {"item": {"value": e + "Q1"}, "k": {"value": "P108"}, "v": {"value": e + "Q9"}},
                {"item": {"value": e + "Q1"}, "k": {"value": "P108"}, "v": {"value": e + "Q9"}},
                {
                    "item": {"value": e + "Q9"},
                    "k": {"value": "P2910"},
                    "v": {"value": "http://commons.wikimedia.org/wiki/Special:FilePath/Other.svg"},
                },
                {
                    "item": {"value": e + "Q9"},
                    "k": {"value": "P8972"},
                    "v": {"value": "http://commons.wikimedia.org/wiki/Special:FilePath/A%20b.svg"},
                },
                {
                    "item": {"value": e + "Q8"},
                    "k": {"value": "P2910"},
                    "v": {"value": "http://commons.wikimedia.org/wiki/Special:FilePath/Icon.png"},
                },
            ]
        }
    }
    items = parse_details(details)
    assert items["Q1"].types == ("Q5",)
    assert items["Q1"].occupations == ("Q901",)
    assert items["Q1"].label == "Marie"
    assert items["Q1"].links == (("P108", "Q9"),)
    assert items["Q9"].logo == "A b.svg"  # P8972 wins over P2910
    assert items["Q8"].logo == "Icon.png"


def test_actor_permissions() -> None:
    anon = Actor.anonymous()
    assert anon.can(Permission.BADGES_READ_PUBLIC) and anon.can(Permission.BADGES_SUBMIT)
    assert not anon.can(Permission.BADGES_MANAGE)
    org = Actor.organization("00000000-0000-0000-0000-000000000001")
    assert org.kind is ActorKind.ORGANIZATION and org.can(Permission.BADGES_ORG_MANAGE)
    assert not org.can(Permission.BADGES_READ)
    assert org.label == "organization:00000000-0000-0000-0000-000000000001"
    assert Permission.BADGES_ORG_MANAGE not in GRANTABLE_PERMISSIONS
    assert Permission.BADGES_MANAGE in GRANTABLE_PERMISSIONS


async def test_x_get_accounts_parses_verified_type() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["ids"] = request.url.params["ids"]
        seen["fields"] = request.url.params["user.fields"]
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "1", "username": "gouv", "name": "Gouv", "verified_type": "government"},
                    {"id": "2", "username": "b", "name": "B", "verified_type": "blue"},
                ],
                "errors": [{"value": "3", "title": "Not Found Error"}],
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://x")
    client = XPlatformClient("token", http=http)
    accounts = await client.get_accounts(["1", "2", "3", "@nope"])
    assert seen == {"path": "/2/users", "ids": "1,2,3", "fields": seen["fields"]}
    assert "verified_type" in seen["fields"]
    assert [(a.external_id, a.verified_type) for a in accounts] == [
        ("1", "government"),
        ("2", "blue"),
    ]
    with pytest.raises(ValueError, match="at most"):
        await client.get_accounts([str(i) for i in range(101)])
    await http.aclose()
