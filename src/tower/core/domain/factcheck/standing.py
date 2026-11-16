"""Standing of a source: who publishes it, and how far it can be relied on for a claim.

"Unknown" is not "unreliable". A domain absent from the human registry is ``unrated``,
not ``tertiary``, and its standing is resolved in this order (docs/factcheck/sources.md):

1. the human registry (``source_registry``) always has the last word;
2. the **official site of the claim's subject** (``first_party``, per passage: Claude →
   Anthropic → ``anthropic.com``), resolved from the subject's Wikidata QID, never guessed
   by a model. Primary, but a stakeholder for a record, conduct or accusation claim;
3. the **domain identity table** (``domain_identities``, from Wikidata "official
   website"): official bodies are primary, universities and journals secondary,
   identified press stays ``unrated`` (being identified says nothing on its editorial
   process);
4. **closed suffix rules** (``.gov``, ``.gouv.fr``, ``.europa.eu``, ``.int``, ``.edu``,
   ``.ac.uk``…), the same for every country;
5. otherwise ``unrated``; the judge then qualifies the source (``JudgedStanding``) in the
   output it already writes. A judged standing is display only: it is capped at
   ``secondary`` and never makes a primary source, so it cannot change the verdict rule.

Pure domain: standard library only. Nothing here depends on a political orientation.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from tower.core.domain.factcheck.values import STAKEHOLDER_CLAIM_TYPES, ClaimType

__all__ = [
    "IDENTITY_KINDS",
    "IDENTITY_RANK",
    "SHARED_HOSTS",
    "SOURCE_TIERS",
    "STANDINGS",
    "TIER_BASES",
    "IdentityKind",
    "JudgedStanding",
    "Standing",
    "TierBasis",
    "effective_tier",
    "first_party_is_stakeholder",
    "host_matches",
    "host_suffixes",
    "identity_standing",
    "is_shared_host",
    "judged_tier",
    "registry_standing",
    "suffix_standing",
]

SOURCE_TIERS = ("primary", "secondary", "tertiary", "avoid", "unrated")
"""Tiers of a retrieved source; ``unrated`` = not assessed (the registry has no
``unrated``: a registry entry is always a human decision)."""


class Standing(StrEnum):
    """Who publishes the source (descriptive, shown to reviewers and readers)."""

    OFFICIAL = "official"
    """Government, administration, court, parliament, statistics office, central bank,
    intergovernmental organization."""
    ACADEMIC = "academic"
    """University, research institute, academy, learned society."""
    JOURNAL = "journal"
    """Scientific journal or scholarly publisher."""
    PRESS = "press"
    """Newspaper, magazine, broadcaster, news agency, news site."""
    FACT_CHECKER = "fact_checker"
    REFERENCE = "reference"
    """Encyclopedia, dictionary, specialist reference work (mathematics, law, medicine…)."""
    ORGANIZATION = "organization"
    """Official site of a company, NGO, association, party or product."""
    USER_GENERATED = "user_generated"
    """Blog, forum, social network, personal page."""
    COMMERCIAL = "commercial"
    """Shop, advertising, content farm."""
    FIRST_PARTY = "first_party"
    """Official site of the claim's subject (per passage, never from the judge)."""
    UNCLEAR = "unclear"


STANDINGS = tuple(s.value for s in Standing)


class JudgedStanding(StrEnum):
    """Standing the judge may give to an unrated source (no ``first_party``: that one is
    resolved deterministically from the subject's QID)."""

    OFFICIAL = "official"
    ACADEMIC = "academic"
    JOURNAL = "journal"
    PRESS = "press"
    FACT_CHECKER = "fact_checker"
    REFERENCE = "reference"
    ORGANIZATION = "organization"
    USER_GENERATED = "user_generated"
    COMMERCIAL = "commercial"
    UNCLEAR = "unclear"


class TierBasis(StrEnum):
    """How the tier of a source was decided."""

    REGISTRY = "registry"
    ADAPTER = "adapter"
    """Declared by an integrated source (Wikipedia, Légifrance, INSEE…)."""
    IDENTITY = "identity"
    SUFFIX = "suffix"
    DEFAULT = "default"


TIER_BASES = tuple(b.value for b in TierBasis)


class IdentityKind(StrEnum):
    """Kind of a ``domain_identities`` row (from the Wikidata class of the entity)."""

    OFFICIAL = "official"
    ACADEMIC = "academic"
    JOURNAL = "journal"
    PRESS = "press"


IDENTITY_KINDS = tuple(k.value for k in IdentityKind)

_IDENTITY: dict[str, tuple[str, Standing]] = {
    IdentityKind.OFFICIAL: ("primary", Standing.OFFICIAL),
    IdentityKind.ACADEMIC: ("secondary", Standing.ACADEMIC),
    IdentityKind.JOURNAL: ("secondary", Standing.JOURNAL),
    IdentityKind.PRESS: ("unrated", Standing.PRESS),
}

IDENTITY_RANK: dict[str, int] = {
    IdentityKind.OFFICIAL.value: 4,
    IdentityKind.ACADEMIC.value: 3,
    IdentityKind.JOURNAL.value: 2,
    IdentityKind.PRESS.value: 1,
}
"""When several entities claim the same host, the highest rank wins."""

_REGISTRY_KIND: dict[str, Standing] = {
    "institution": Standing.OFFICIAL,
    "statistics": Standing.OFFICIAL,
    "court": Standing.OFFICIAL,
    "parliament": Standing.OFFICIAL,
    "agency": Standing.OFFICIAL,
    "press": Standing.PRESS,
    "encyclopedia": Standing.REFERENCE,
    "fact_checker": Standing.FACT_CHECKER,
    "academic": Standing.ACADEMIC,
    "platform": Standing.USER_GENERATED,
}

_JUDGED_SECONDARY = frozenset(
    {
        JudgedStanding.OFFICIAL,
        JudgedStanding.ACADEMIC,
        JudgedStanding.JOURNAL,
        JudgedStanding.REFERENCE,
        JudgedStanding.FACT_CHECKER,
    }
)
_JUDGED_TERTIARY = frozenset({JudgedStanding.USER_GENERATED, JudgedStanding.COMMERCIAL})

# Closed suffix rules (host labels, compared from the right). ``cc`` = any two-letter
# country code. Same rule for every country.
_OFFICIAL_SUFFIXES = (
    ("gov",),
    ("mil",),
    ("int",),
    ("gouv", "fr"),
    ("europa", "eu"),
    ("gc", "ca"),
    ("admin", "ch"),
    ("fgov", "be"),
    ("bund", "de"),
    ("gv", "at"),
    ("govt", "nz"),
    ("gub", "uy"),
    ("gov", "cc"),
    ("gouv", "cc"),
    ("gob", "cc"),
    ("go", "cc"),
)
_ACADEMIC_SUFFIXES = (
    ("edu",),
    ("edu", "cc"),
    ("ac", "cc"),
)

SHARED_HOSTS = frozenset(
    {
        "facebook.com",
        "fb.com",
        "instagram.com",
        "twitter.com",
        "x.com",
        "t.co",
        "youtube.com",
        "youtu.be",
        "tiktok.com",
        "linkedin.com",
        "t.me",
        "telegram.me",
        "vk.com",
        "ok.ru",
        "weibo.com",
        "reddit.com",
        "threads.net",
        "bsky.app",
        "mastodon.social",
        "google.com",
        "goo.gl",
        "blogspot.com",
        "blogger.com",
        "wordpress.com",
        "medium.com",
        "substack.com",
        "tumblr.com",
        "wix.com",
        "wixsite.com",
        "weebly.com",
        "squarespace.com",
        "jimdo.com",
        "jimdofree.com",
        "webnode.com",
        "over-blog.com",
        "canalblog.com",
        "hautetfort.com",
        "github.com",
        "github.io",
        "gitlab.com",
        "gitlab.io",
        "sourceforge.net",
        "notion.site",
        "about.me",
        "linktr.ee",
        "archive.org",
        "wikipedia.org",
        "wikimedia.org",
        "wikidata.org",
        "soundcloud.com",
        "bandcamp.com",
        "flickr.com",
        "issuu.com",
        "calameo.com",
        "academia.edu",
        "researchgate.net",
        "free.fr",
        "pagesperso-orange.fr",
        "oclc.org",
        "worldcat.org",
        "hathitrust.org",
        "ebscohost.com",
        "proquest.com",
        "doi.org",
        "handle.net",
        "issn.org",
    }
)
"""Hosting platforms and catalogs: an official website on one of them says nothing about
the platform (``facebook.com/some-ministry``, a library catalog for a journal). Never
recorded as an identity, never matched by the suffix rules (``academia.edu`` is a social
network, not a university)."""


def host_suffixes(host: str) -> list[str]:
    """``a.b.example.org`` → ``[a.b.example.org, b.example.org, example.org]`` (most
    specific first, never a bare top-level label)."""
    labels = [x for x in host.lower().strip(".").removeprefix("www.").split(".") if x]
    return [".".join(labels[i:]) for i in range(max(len(labels) - 1, 1))]


def is_shared_host(host: str) -> bool:
    """The host is (under) a hosting platform of :data:`SHARED_HOSTS`."""
    return any(s in SHARED_HOSTS for s in host_suffixes(host))


def host_matches(host: str, sites: Iterable[str]) -> bool:
    """``host`` is one of ``sites`` or a subdomain of one (``docs.anthropic.com`` matches
    ``anthropic.com``). A shared platform never matches."""
    if is_shared_host(host):
        return False
    wanted = {s.lower().removeprefix("www.") for s in sites if s}
    return any(s in wanted for s in host_suffixes(host))


def _match(labels: list[str], suffix: tuple[str, ...]) -> bool:
    if len(labels) <= len(suffix):  # the bare suffix is not a site
        return False
    tail = labels[-len(suffix) :]
    for got, want in zip(tail, suffix, strict=True):
        if want == "cc":
            if len(got) != 2 or not got.isalpha():
                return False
        elif got != want:
            return False
    return True


def suffix_standing(host: str) -> tuple[str, Standing] | None:
    """Closed suffix rules: official suffixes → ``("primary", official)``, academic
    suffixes → ``("secondary", academic)``; ``None`` otherwise or on a shared platform."""
    if is_shared_host(host):
        return None
    labels = [x for x in host.lower().strip(".").removeprefix("www.").split(".") if x]
    if any(_match(labels, s) for s in _OFFICIAL_SUFFIXES):
        return "primary", Standing.OFFICIAL
    if any(_match(labels, s) for s in _ACADEMIC_SUFFIXES):
        return "secondary", Standing.ACADEMIC
    return None


def identity_standing(kind: str) -> tuple[str, Standing] | None:
    """Tier and standing of a ``domain_identities`` kind (``None`` for an unknown kind)."""
    return _IDENTITY.get(kind)


def registry_standing(kind: str | None) -> Standing | None:
    """Standing implied by a registry ``kind`` (``other`` and unknown → ``None``)."""
    return _REGISTRY_KIND.get(kind or "")


def judged_tier(source_tier: str, judged: str | None) -> str:
    """Display tier of an ``unrated`` source once the judge qualified it: official,
    academic, journal, reference and fact-checker → ``secondary`` (never higher); user
    content and commercial → ``tertiary``; otherwise unchanged. Any other tier is kept."""
    if source_tier != "unrated" or judged is None:
        return source_tier
    if judged in _JUDGED_SECONDARY:
        return "secondary"
    if judged in _JUDGED_TERTIARY:
        return "tertiary"
    return source_tier


def effective_tier(source_tier: str, passage_standing: str | None) -> str:
    """Tier of a passage for the independence rule and the display: the official site of
    the claim's subject is ``primary`` (unless the source is ``avoid``); a judged standing
    refines an ``unrated`` source (:func:`judged_tier`)."""
    if passage_standing == Standing.FIRST_PARTY:
        return source_tier if source_tier == "avoid" else "primary"
    return judged_tier(source_tier, passage_standing)


def first_party_is_stakeholder(claim_type: str) -> bool:
    """The subject's own site is a party to a record, conduct or accusation claim about
    the subject (closed rule, as for registry stakeholders): it is then evidence of its
    own position, not an independent confirmation."""
    try:
        return ClaimType(claim_type) in STAKEHOLDER_CLAIM_TYPES
    except ValueError:
        return False
