"""External sources of the badge service: Wikidata (SPARQL) and X account lookups."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from tower.core.ports.platforms import PublicAccount


@dataclass(frozen=True, slots=True)
class WikidataAccount:
    """A Wikidata item and the X numeric id of one of its accounts (P2002 + P6552)."""

    qid: str
    x_id: str
    handle: str | None = None


@dataclass(frozen=True, slots=True)
class WikidataItem:
    qid: str
    label: str | None = None
    types: tuple[str, ...] = ()
    """P31 values (QIDs)."""
    occupations: tuple[str, ...] = ()
    """P106 values (QIDs)."""
    logo: str | None = None
    """P154 Commons file name."""
    links: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    """(property, organisation QID) of P108/P102/P54/P463 statements without end time."""


class WikidataBadgeSource(Protocol):
    """Wikidata Query Service. Raises ``SourceUnavailable`` on any failure."""

    async def x_accounts(self, scope: str) -> Sequence[WikidataAccount]:
        """Items of a scope (country QID, or ``intl`` for international organisations)
        with a current, non-deprecated X account carrying its numeric id."""
        ...

    async def items(self, qids: Sequence[str]) -> Mapping[str, WikidataItem]:
        """Types, occupations, French label, logo and affiliation links (≤ 200 QIDs)."""
        ...

    async def aclose(self) -> None: ...


class AccountBatchClient(Protocol):
    """Batch account lookup of a platform (X: ``GET /2/users?ids=``, ≤ 100 ids)."""

    platform: str
    cost_per_account_read_usd_micros: int

    async def get_accounts(self, external_ids: Sequence[str]) -> Sequence[PublicAccount]: ...
