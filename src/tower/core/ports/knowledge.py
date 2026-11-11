"""Knowledge sources (Atlas). Contract only: adapters come with the fact-check lots."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class Tier(StrEnum):
    PRIMARY = "primary"
    SECONDARY = "secondary"
    TERTIARY = "tertiary"


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


class KnowledgeSource(Protocol):
    name: str
    capabilities: SourceCapabilities
    reliability_tier: Tier
    cost_per_call_usd_micros: int
    license: str

    async def lookup(self, query: SourceQuery) -> Sequence[Evidence]: ...
