"""Knowledge sources (Atlas): contracts of the adapters in ``core/adapters/knowledge``."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from tower.core.domain.error_codes import ErrorCode


class Tier(StrEnum):
    PRIMARY = "primary"
    SECONDARY = "secondary"
    TERTIARY = "tertiary"
    AVOID = "avoid"


@dataclass(frozen=True, slots=True)
class SourceCapabilities:
    claim_types: frozenset[str] = frozenset()
    jurisdictions: frozenset[str] = frozenset()
    """ISO 3166-1/2 codes, ``EU``, ``INT``; empty = any."""
    languages: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class SourceQuery:
    text: str
    lang: str
    claim_type: str | None = None
    jurisdiction: str | None = None
    subject_qid: str | None = None
    limit: int = 5
    as_of: datetime | None = None
    """Date the claim refers to (law in force, statistic period); ``None`` = today."""


@dataclass(frozen=True, slots=True)
class Evidence:
    source: str
    """Adapter name (``wikidata``, ``legi_fr``…)."""
    stable_id: str
    """QID, LEGI article id, INSEE series code, Wikipedia revision…"""
    url: str
    text: str
    lang: str
    license: str
    sha256: str
    published_at: datetime | None = None
    version: str | None = None
    extra: dict[str, str] = field(default_factory=dict)
    title: str | None = None
    publisher: str | None = None
    tier: Tier | None = None
    """Tier declared by the adapter (Wikidata value without reference → tertiary);
    ``None`` = from the registry, by domain."""
    references: tuple[str, ...] = ()
    """URLs cited by the evidence (Wikipedia references, Wikidata reference URLs)."""


class KnowledgeSource(Protocol):
    name: str
    capabilities: SourceCapabilities
    reliability_tier: Tier
    cost_per_call_usd_micros: int
    license: str

    async def lookup(self, query: SourceQuery) -> Sequence[Evidence]: ...


class SourceCache(Protocol):
    """``source_cache`` table: a fresh response is never requested (or paid) twice."""

    async def get(self, adapter: str, request: Mapping[str, object]) -> dict[str, Any] | None: ...

    async def put(
        self,
        adapter: str,
        request: Mapping[str, object],
        response: Mapping[str, Any],
        *,
        ttl_s: int,
    ) -> None: ...


class SourceUnavailable(Exception):
    """An adapter failed; ``code`` is the stable ``TWR-ATL-*`` entry. The caller skips the
    source and carries on (an unavailable source never blocks an investigation)."""

    def __init__(self, message: str, *, code: ErrorCode, source: str) -> None:
        super().__init__(message)
        self.code = code
        self.source = source
