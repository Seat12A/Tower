"""Seat 12A verification badges and affiliations. Standard library only.

A badge belongs to one platform account, identified by its native id on the platform
(X numeric id, ...), never by its handle. One badge per account; when an account
qualifies for several categories, ``pick_category`` applies the priority order.

An affiliation links an individual account to an organisation account that holds an
affiliable badge (organisation, media, institutional). The affiliated account shows the
organisation's colour and logo, unless it has its own badge.

The Wikidata classification tables below are deliberately conservative: an item whose
types and occupations are not listed gets no badge (in doubt, no badge).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from tower.core.domain import error_codes as ec
from tower.core.domain.errors import InvalidInput
from tower.core.domain.platforms import Platform, parse_platform, validate_external_id


class BadgeCategory(StrEnum):
    """Badge category (API values in English; French names in docs/badges.md)."""

    INSTITUTIONAL = "institutional"  # institutionnel
    ORGANIZATION = "organization"  # organisation
    MEDIA = "media"  # media
    EXPERT = "expert"  # expert
    PERSONALITY = "personality"  # personnalite


CATEGORY_COLORS: dict[BadgeCategory, str] = {
    BadgeCategory.INSTITUTIONAL: "#FF7A00",
    BadgeCategory.ORGANIZATION: "#FFD60A",
    BadgeCategory.MEDIA: "#D946EF",
    BadgeCategory.EXPERT: "#22D3EE",
    BadgeCategory.PERSONALITY: "#3B5BFF",
}

CATEGORY_PRIORITY: tuple[BadgeCategory, ...] = (
    BadgeCategory.INSTITUTIONAL,
    BadgeCategory.MEDIA,
    BadgeCategory.EXPERT,
    BadgeCategory.ORGANIZATION,
    BadgeCategory.PERSONALITY,
)
"""Highest first: institutional > media > expert > organization > personality."""

AFFILIABLE_CATEGORIES = frozenset(
    {BadgeCategory.ORGANIZATION, BadgeCategory.MEDIA, BadgeCategory.INSTITUTIONAL}
)
"""Categories of an organisation account that can have affiliated accounts."""


class BadgeStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    BLOCKED = "blocked"
    """No badge, and automatic sources (Wikidata, X) never grant one until unblocked."""


class BadgeSource(StrEnum):
    MANUAL = "manual"  # granted by the team
    REQUEST = "request"  # approved badge request
    WIKIDATA = "wikidata"  # Wikidata import
    X_GOVERNMENT = "x_government"  # X "government" verification

    @property
    def automatic(self) -> bool:
        return self in (BadgeSource.WIKIDATA, BadgeSource.X_GOVERNMENT)


class AffiliationStatus(StrEnum):
    PENDING = "pending"  # waiting for the organisation or the team
    ACTIVE = "active"
    REJECTED = "rejected"
    REVOKED = "revoked"
    EXPIRED = "expired"  # not reconfirmed within AFFILIATION_VALIDITY


LIVE_AFFILIATION_STATUSES = frozenset({AffiliationStatus.PENDING, AffiliationStatus.ACTIVE})


class AffiliationSource(StrEnum):
    MANUAL = "manual"  # created by the team
    ORGANIZATION = "organization"  # created by the organisation itself
    WIKIDATA = "wikidata"
    REQUEST = "request"  # badge request of kind "affiliation"


class RequestKind(StrEnum):
    BADGE = "badge"
    AFFILIATION = "affiliation"


class RequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ReportReason(StrEnum):
    WRONG_CATEGORY = "wrong_category"
    NOT_ELIGIBLE = "not_eligible"
    IMPERSONATION = "impersonation"
    WRONG_AFFILIATION = "wrong_affiliation"
    OTHER = "other"


class ReportStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class BadgeAction(StrEnum):
    """Actions recorded in the ``badge_events`` history."""

    GRANTED = "granted"
    UPDATED = "updated"
    REVOKED = "revoked"
    BLOCKED = "blocked"
    UNBLOCKED = "unblocked"
    AFFILIATION_CREATED = "affiliation_created"
    AFFILIATION_VALIDATED = "affiliation_validated"
    AFFILIATION_RECONFIRMED = "affiliation_reconfirmed"
    AFFILIATION_REJECTED = "affiliation_rejected"
    AFFILIATION_REVOKED = "affiliation_revoked"
    AFFILIATION_EXPIRED = "affiliation_expired"
    AFFILIATION_PRIMARY = "affiliation_primary"
    ORG_KEY_CREATED = "org_key_created"
    ORG_KEY_REVOKED = "org_key_revoked"


AFFILIATION_VALIDITY = timedelta(days=365)
"""An active affiliation expires one year after its last validation or reconfirmation."""

MAX_LOOKUP_IDS = 100
MAX_LABEL = 120
MAX_TITLE = 120
MAX_NOTE = 2000
MAX_JUSTIFICATION = 4000
MAX_CONTACT = 320
MAX_COMMENT = 2000
MAX_URL = 2048
MAX_EVIDENCE_URLS = 10


def pick_category(categories: Iterable[BadgeCategory | str]) -> BadgeCategory | None:
    """Highest-priority category among ``categories`` (None when empty)."""
    present = {BadgeCategory(c) for c in categories}
    for category in CATEGORY_PRIORITY:
        if category in present:
            return category
    return None


def parse_category(value: str) -> BadgeCategory:
    try:
        return BadgeCategory(value)
    except ValueError:
        raise InvalidInput(
            "Unknown badge category", code=ec.BDG_BAD_CATEGORY, category=str(value)[:64]
        ) from None


def color_of(category: BadgeCategory | str) -> str:
    return CATEGORY_COLORS[BadgeCategory(category)]


def affiliation_expiry(validated_at: datetime) -> datetime:
    return validated_at + AFFILIATION_VALIDITY


@dataclass(frozen=True, slots=True)
class AccountRef:
    """Platform account reference: ``platform`` + native id (never a handle)."""

    platform: Platform
    external_id: str

    @classmethod
    def parse(cls, platform: str, external_id: str) -> AccountRef:
        plat = parse_platform(platform)
        return cls(plat, validate_external_id(plat, external_id))


def parse_lookup_ids(platform: str, raw: Iterable[str]) -> tuple[Platform, list[str]]:
    """Validate a lookup batch: known platform, 1 to ``MAX_LOOKUP_IDS`` valid ids.

    Duplicates are removed; the order of first appearance is kept.
    """
    plat = parse_platform(platform)
    out: list[str] = []
    seen: set[str] = set()
    for value in raw:
        value = value.strip()
        if not value or value in seen:
            continue
        validate_external_id(plat, value)
        seen.add(value)
        out.append(value)
    if not out:
        raise InvalidInput("At least one id is required", code=ec.BDG_BAD_LOOKUP)
    if len(out) > MAX_LOOKUP_IDS:
        raise InvalidInput(
            "Too many ids in the lookup", code=ec.BDG_BAD_LOOKUP, max_ids=MAX_LOOKUP_IDS
        )
    return plat, out


def check_text(value: str | None, *, field: str, max_chars: int) -> str | None:
    """Strip; empty → None; too long → ``InvalidInput`` (``TWR-BDG-010``)."""
    if value is None:
        return None
    value = value.strip()
    if len(value) > max_chars:
        raise InvalidInput("Text too long", code=ec.BDG_BAD_TEXT, field=field, max_chars=max_chars)
    return value or None


def check_url(value: str | None, *, field: str) -> str | None:
    """Only absolute ``https://`` URLs (logos, evidence); empty → None."""
    value = check_text(value, field=field, max_chars=MAX_URL)
    if value is None:
        return None
    if not value.startswith("https://") or any(c.isspace() for c in value):
        raise InvalidInput("An https:// URL is required", code=ec.BDG_BAD_TEXT, field=field)
    return value


# --- Wikidata --------------------------------------------------------------------------

WIKIDATA_HUMAN = "Q5"

WIKIDATA_AFFILIATION_PROPERTIES: dict[str, str] = {
    "P108": "employer",
    "P102": "member of political party",
    "P54": "member of sports team",
    "P463": "member of",
}
"""Statements read for affiliations; a statement with an end time (P582) is ignored."""

WIKIDATA_OCCUPATIONS: dict[str, BadgeCategory] = {
    # institutional: elected officials and representatives
    "Q82955": BadgeCategory.INSTITUTIONAL,  # politician
    "Q486839": BadgeCategory.INSTITUTIONAL,  # member of parliament
    "Q30185": BadgeCategory.INSTITUTIONAL,  # mayor
    "Q193391": BadgeCategory.INSTITUTIONAL,  # diplomat
    # media
    "Q1930187": BadgeCategory.MEDIA,  # journalist
    "Q42909": BadgeCategory.MEDIA,  # reporter
    "Q1155838": BadgeCategory.MEDIA,  # correspondent
    "Q947873": BadgeCategory.MEDIA,  # television presenter
    "Q13590141": BadgeCategory.MEDIA,  # presenter
    "Q1371925": BadgeCategory.MEDIA,  # announcer
    "Q1607826": BadgeCategory.MEDIA,  # editor
    # expert
    "Q901": BadgeCategory.EXPERT,  # scientist
    "Q1650915": BadgeCategory.EXPERT,  # researcher
    "Q1622272": BadgeCategory.EXPERT,  # university teacher
    "Q39631": BadgeCategory.EXPERT,  # physician
    "Q169470": BadgeCategory.EXPERT,  # physicist
    "Q170790": BadgeCategory.EXPERT,  # mathematician
    "Q188094": BadgeCategory.EXPERT,  # economist
    "Q11063": BadgeCategory.EXPERT,  # astronomer
    "Q864503": BadgeCategory.EXPERT,  # biologist
    # personality
    "Q483501": BadgeCategory.PERSONALITY,  # artist
    "Q33999": BadgeCategory.PERSONALITY,  # actor
    "Q10800557": BadgeCategory.PERSONALITY,  # film actor
    "Q2526255": BadgeCategory.PERSONALITY,  # film director
    "Q28389": BadgeCategory.PERSONALITY,  # screenwriter
    "Q177220": BadgeCategory.PERSONALITY,  # singer
    "Q488205": BadgeCategory.PERSONALITY,  # singer-songwriter
    "Q753110": BadgeCategory.PERSONALITY,  # songwriter
    "Q639669": BadgeCategory.PERSONALITY,  # musician
    "Q130857": BadgeCategory.PERSONALITY,  # disc jockey
    "Q36180": BadgeCategory.PERSONALITY,  # writer
    "Q6625963": BadgeCategory.PERSONALITY,  # novelist
    "Q49757": BadgeCategory.PERSONALITY,  # poet
    "Q1028181": BadgeCategory.PERSONALITY,  # painter
    "Q245068": BadgeCategory.PERSONALITY,  # comedian
    "Q4610556": BadgeCategory.PERSONALITY,  # model
    "Q17125263": BadgeCategory.PERSONALITY,  # YouTuber
    "Q15077007": BadgeCategory.PERSONALITY,  # podcaster
    "Q2066131": BadgeCategory.PERSONALITY,  # athlete
    "Q937857": BadgeCategory.PERSONALITY,  # association football player
    "Q3665646": BadgeCategory.PERSONALITY,  # basketball player
    "Q10833314": BadgeCategory.PERSONALITY,  # tennis player
    "Q11774891": BadgeCategory.PERSONALITY,  # ice hockey player
}
"""Occupation (P106) of a human → category. QIDs checked against Wikidata labels."""

WIKIDATA_TYPES: dict[str, BadgeCategory] = {
    # institutional
    "Q7278": BadgeCategory.INSTITUTIONAL,  # political party
    "Q484652": BadgeCategory.INSTITUTIONAL,  # international organization
    "Q245065": BadgeCategory.INSTITUTIONAL,  # intergovernmental organization
    "Q327333": BadgeCategory.INSTITUTIONAL,  # government agency
    "Q2659904": BadgeCategory.INSTITUTIONAL,  # government organization
    "Q7188": BadgeCategory.INSTITUTIONAL,  # government
    "Q192350": BadgeCategory.INSTITUTIONAL,  # ministry
    "Q11204": BadgeCategory.INSTITUTIONAL,  # legislature
    "Q3917681": BadgeCategory.INSTITUTIONAL,  # embassy
    "Q15284": BadgeCategory.INSTITUTIONAL,  # municipality
    # media
    "Q11032": BadgeCategory.MEDIA,  # newspaper
    "Q1110794": BadgeCategory.MEDIA,  # daily newspaper
    "Q41298": BadgeCategory.MEDIA,  # magazine
    "Q192283": BadgeCategory.MEDIA,  # news agency
    "Q2001305": BadgeCategory.MEDIA,  # television channel
    "Q1616075": BadgeCategory.MEDIA,  # television station
    "Q14350": BadgeCategory.MEDIA,  # radio station
    "Q15265344": BadgeCategory.MEDIA,  # broadcaster
    "Q1193236": BadgeCategory.MEDIA,  # news media
    "Q11033": BadgeCategory.MEDIA,  # mass media
    "Q17232649": BadgeCategory.MEDIA,  # news website
    "Q1331793": BadgeCategory.MEDIA,  # media company
    # organization
    "Q4830453": BadgeCategory.ORGANIZATION,  # business
    "Q783794": BadgeCategory.ORGANIZATION,  # company
    "Q163740": BadgeCategory.ORGANIZATION,  # nonprofit organization
    "Q79913": BadgeCategory.ORGANIZATION,  # non-governmental organization
    "Q48204": BadgeCategory.ORGANIZATION,  # voluntary association
    "Q15911314": BadgeCategory.ORGANIZATION,  # association
    "Q476028": BadgeCategory.ORGANIZATION,  # association football club
    "Q847017": BadgeCategory.ORGANIZATION,  # sports club
    "Q4438121": BadgeCategory.ORGANIZATION,  # sports organization
    "Q3918": BadgeCategory.ORGANIZATION,  # university
    "Q875538": BadgeCategory.ORGANIZATION,  # public university
    "Q2085381": BadgeCategory.ORGANIZATION,  # publishing house
}
"""Type (P31) of a non-human item → category. QIDs checked against Wikidata labels."""


def classify_wikidata(types: Iterable[str], occupations: Iterable[str]) -> BadgeCategory | None:
    """Category of a Wikidata item, or None when nothing listed matches.

    A human (P31 = Q5) is classified by its occupations (P106) only; any other item by
    its types (P31) only. Several matches → ``pick_category``.
    """
    type_set = set(types)
    if WIKIDATA_HUMAN in type_set:
        return pick_category(
            WIKIDATA_OCCUPATIONS[q] for q in occupations if q in WIKIDATA_OCCUPATIONS
        )
    return pick_category(WIKIDATA_TYPES[q] for q in type_set if q in WIKIDATA_TYPES)


def is_qid(value: str) -> bool:
    return len(value) >= 2 and value[0] == "Q" and value[1:].isdigit() and value[1] != "0"


def commons_file_url(file_name: str) -> str | None:
    """https URL of a Wikimedia Commons file (logo P154), None if the name is unusable."""
    name = file_name.strip().replace(" ", "_")
    if not name or len(name) > 240 or "/" in name or not name.isprintable():
        return None
    from urllib.parse import quote

    return "https://commons.wikimedia.org/wiki/Special:FilePath/" + quote(name)


# --- X ---------------------------------------------------------------------------------

X_GOVERNMENT_VERIFIED_TYPE = "government"
"""X API v2 ``verified_type`` of accounts verified as government or multilateral
organisations (grey check mark)."""
