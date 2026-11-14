"""Closed values and pure rules of the complete Atlas (lot 6): country registries, legal
mirror, news index, editable reliability registry and the stakeholder rule (E7, Q11).

Pure domain: standard library only. Values are stored as ``TEXT`` + ``CHECK``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from tower.core.domain.factcheck.values import STAKEHOLDER_CLAIM_TYPES, ClaimType, EntityRole

__all__ = [
    "LEGI_OPEN_END",
    "LEGI_PRIORITY_TEXTS",
    "PARTY_ROLES",
    "ControlOrigin",
    "ElectionKind",
    "IndependenceBasis",
    "JurisdictionSourceType",
    "LegalAccess",
    "NewsFeedKind",
    "NewsOrigin",
    "PublisherIdentity",
    "RegistryChange",
    "is_stakeholder",
    "legi_date",
    "normalize_entity",
    "valid_country",
]


class JurisdictionSourceType(StrEnum):
    """Kind of a country source (``jurisdiction_sources.source_type``)."""

    STATISTICS = "statistics"
    LAW = "law"
    ELECTORAL = "electoral"
    CENTRAL_BANK = "central_bank"
    HEALTH = "health"
    JUSTICE = "justice"
    NEWS = "news"
    OTHER = "other"


class LegalAccess(StrEnum):
    """How a legal portal can be read (``legal_portals.access``)."""

    BULK = "bulk"  # downloadable dump (LEGI, Lois du Canada XML)
    API = "api"  # documented API or SPARQL endpoint (EUR-Lex, Fedlex)
    WEB = "web"  # website only: web search restricted to its domain
    UNVERIFIED = "unverified"  # automated access not confirmed (Justel, Legilux…)


class NewsFeedKind(StrEnum):
    """Editorial category of a reference news feed (neutrality: the seed covers all)."""

    PUBLIC_SERVICE = "public_service"
    NATIONAL = "national"
    REGIONAL = "regional"
    AGENCY = "agency"
    INTERNATIONAL = "international"


class NewsOrigin(StrEnum):
    """Where a ``news_index`` row comes from."""

    RSS = "rss"
    GDELT = "gdelt"


class ElectionKind(StrEnum):
    PRESIDENTIAL = "presidential"
    LEGISLATIVE = "legislative"
    EUROPEAN = "european"
    LOCAL = "local"
    REGIONAL = "regional"
    REFERENDUM = "referendum"
    OTHER = "other"


class ControlOrigin(StrEnum):
    """Who set ``source_registry.controlled_by``."""

    WIKIDATA = "wikidata"  # owner (P127) / parent organization (P749), automatic seed
    TEAM = "team"  # corrected or set by an editor
    OFFICIAL = "official"  # official register (state-owned company lists…)


class IndependenceBasis(StrEnum):
    """External criterion behind ``statutory_independence`` (never a team judgement)."""

    IMF_SDDS = "imf_sdds"  # subscriber to the IMF Special Data Dissemination Standard
    IMF_SDDS_PLUS = "imf_sdds_plus"
    ESS_PEER_REVIEW = "ess_peer_review"  # European Statistical System peer review
    STATUTE = "statute"  # independence written in law (statute cited in the reason)


class RegistryChange(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    DEACTIVATED = "deactivated"


LEGI_PRIORITY_TEXTS: Mapping[str, str] = {
    "LEGITEXT000006070719": "Code pénal",
    "LEGITEXT000006071154": "Code de procédure pénale",
    "LEGITEXT000006070721": "Code civil",
    "LEGITEXT000006072050": "Code du travail",
    "LEGITEXT000006070158": "Code de l'entrée et du séjour des étrangers et du droit d'asile",
    "LEGITEXT000006070239": "Code électoral",
    "LEGITEXT000006072665": "Code de la santé publique",
    "LEGITEXT000025503132": "Code de la sécurité intérieure",
    # The Constitution sits under its JORF id in the LEGI dump (articles carry cid=JORF…;
    # LEGITEXT000006071194 only names its structure file).
    "JORFTEXT000000571356": "Constitution du 4 octobre 1958",
}
"""Priority French texts of the LEGI mirror (connaissances.md), by LEGI text id."""

LEGI_OPEN_END = date(2999, 1, 1)
"""``DATE_FIN`` of an article still in force in LEGI (stored as ``NULL``)."""

PARTY_ROLES = frozenset({EntityRole.ACTOR, EntityRole.ACCUSER, EntityRole.ACCUSED})
"""Roles that make an entity a party to the claim (author of the conduct, accuser,
accused)."""

_COUNTRY = re.compile(r"^(?:[A-Z]{2}|[A-Z]{2}-[A-Z0-9]{1,3}|EU|INT)$")
_LEGAL_SUFFIXES = re.compile(
    r"\b(?:sa|sas|sasu|sarl|se|plc|ltd|limited|inc|gmbh|ag|nv|bv|spa|group|groupe)\b"
)
_NON_WORD = re.compile(r"[^0-9a-z]+")


def valid_country(code: str) -> bool:
    """ISO 3166-1 alpha-2, ISO 3166-2 subdivision, ``EU`` or ``INT``."""
    return bool(_COUNTRY.fullmatch(code))


def legi_date(raw: str | None) -> date | None:
    """``YYYY-MM-DD`` of LEGI → date; ``2999-01-01`` (open end) and invalid → ``None``."""
    if not raw:
        return None
    try:
        value = date.fromisoformat(raw.strip()[:10])
    except ValueError:
        return None
    return None if value >= LEGI_OPEN_END else value


def normalize_entity(name: str) -> str:
    """Comparable key of an organisation name: no accents, casefolded, legal suffixes
    and punctuation removed (``Groupe TF1 SA`` → ``tf1``)."""
    stripped = unicodedata.normalize("NFKD", name.casefold())
    ascii_only = "".join(c for c in stripped if not unicodedata.combining(c))
    without_suffix = _LEGAL_SUFFIXES.sub(" ", ascii_only)
    return _NON_WORD.sub(" ", without_suffix).strip()


@dataclass(frozen=True, slots=True)
class PublisherIdentity:
    """What the registry knows of a source's publisher (stakeholder join)."""

    names: tuple[str, ...]
    """Publisher name and aliases."""
    controllers: tuple[str, ...] = ()
    """``controlled_by`` (and its aliases): the entity that controls the publisher."""


def is_stakeholder(
    claim_type: str,
    parties: Iterable[tuple[str, str]],
    publisher: PublisherIdentity | None,
) -> bool:
    """Closed stakeholder rule (pipeline.md E7, Q11): the source is a stakeholder when the
    claim is a record, a conduct or an accusation **and** its publisher, or an entity that
    controls it according to the registry, is a party (``(name, role)`` with a role in
    :data:`PARTY_ROLES`). A join on normalized names, never a model judgement; symmetric
    for every kind of party (government, opposition, company, NGO, union)."""
    if publisher is None:
        return False
    try:
        if ClaimType(claim_type) not in STAKEHOLDER_CLAIM_TYPES:
            return False
    except ValueError:
        return False
    party_keys = {
        normalize_entity(name)
        for name, role in parties
        if role in {r.value for r in PARTY_ROLES} and normalize_entity(name)
    }
    if not party_keys:
        return False
    identity = {normalize_entity(n) for n in (*publisher.names, *publisher.controllers)}
    identity.discard("")
    return bool(party_keys & identity)
