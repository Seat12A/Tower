"""Atlas: source selection and collection for one investigation (pipeline.md E7).

Order (connaissances.md): existing fact-checks → structured sources (Wikidata, INSEE,
Légifrance) → Wikipedia (then its references to primary sources) → web search (Brave:
primary domains of the jurisdiction first, then broadened). Every call goes through
``source_cache``; an unavailable source is captured (``TWR-ATL-*``) and skipped.

Pure orchestration: the caller (service ``investigate``) chunks, ranks and stores.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

from tower.core.ports.knowledge import KnowledgeSource, SourceCache
from tower.core.ports.web import SearchClient, WebFetcher

__all__ = [
    "Atlas",
    "AtlasDocument",
    "AtlasQuery",
    "AtlasRequest",
    "AtlasResult",
    "RegistryInfo",
    "registrable_domain",
]


@dataclass(frozen=True, slots=True)
class RegistryInfo:
    """One ``source_registry`` row."""

    domain: str
    publisher: str
    tier: str
    kind: str
    country: str | None


@dataclass(frozen=True, slots=True)
class AtlasQuery:
    query: str
    lang: str
    source_type: str
    target: str
    """``encyclopedia``, ``fact_check`` or ``web``."""
    side: str
    """``confirm`` or ``refute`` (logged: both series must be non-empty)."""


@dataclass(frozen=True, slots=True)
class AtlasRequest:
    claim_text: str
    """Canonical claim, original language (for fact-check search)."""
    claim_lang: str
    claim_type: str
    jurisdictions: tuple[str, ...]
    queries: tuple[AtlasQuery, ...]
    entity_lookups: tuple[str, ...]
    max_web_searches: int
    """4 at the standard depth, +4 at the reinforced depth (round 2)."""
    as_of: date | None = None
    max_documents: int = 24
    max_reference_fetches: int = 6
    """Wikipedia references followed to primary sources, at most."""


@dataclass(frozen=True, slots=True)
class AtlasDocument:
    adapter: str
    canonical_url: str
    original_url: str
    domain: str
    tier: str
    lang: str
    text: str
    """Plain text (HTML already stripped); never kept beyond the investigation unless cited."""
    title: str | None = None
    publisher: str | None = None
    published_at: datetime | None = None
    stable_id: str | None = None
    sha256: str | None = None
    license: str | None = None
    side: str | None = None
    """Query side that found it (``confirm``, ``refute``) or ``None`` (structured lookup)."""


@dataclass(slots=True)
class AtlasResult:
    documents: list[AtlasDocument] = field(default_factory=list)
    web_searches: int = 0
    paid_cost_usd_micros: int = 0
    adapters_used: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    """Adapter → ``TWR-ATL-*`` code of the failure."""


def registrable_domain(host_or_url: str) -> str:
    """``https://www.insee.fr/fr/x`` → ``insee.fr``; keeps known two-level public suffixes
    (``gouv.fr``, ``gov.uk``, ``co.uk``, ``europa.eu`` handled as a suffix match by the
    registry lookup). Lowercase, no port, no ``www.``."""
    raise NotImplementedError


class Atlas:
    """Collects documents for one request. Stateless apart from injected clients."""

    def __init__(
        self,
        *,
        sources: Mapping[str, KnowledgeSource],
        search: SearchClient | None,
        fetcher: WebFetcher | None,
        cache: SourceCache | None,
        registry: Mapping[str, RegistryInfo],
        search_cost_usd_micros: int = 5000,
    ) -> None:
        """``sources`` by name: ``factcheck_tools``, ``wikidata``, ``wikipedia``,
        ``insee``, ``legifrance`` (absent = not configured)."""
        raise NotImplementedError

    def tier_of(self, url_or_domain: str) -> tuple[str, str | None]:
        """``(tier, publisher)`` from the registry (suffix match on the registrable
        domain); ``("tertiary", None)`` when unknown."""
        raise NotImplementedError

    def primary_domains(self, jurisdictions: Sequence[str]) -> list[str]:
        """Primary registry domains of the jurisdictions (plus ``INT`` and ``EU``)."""
        raise NotImplementedError

    async def collect(self, request: AtlasRequest) -> AtlasResult:
        raise NotImplementedError
