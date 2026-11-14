"""Knowledge source over the local news index (``news_index``): recent headlines.

Full-text search (PostgreSQL ``simple`` configuration) over title + lead of the last 30
days before ``query.as_of`` (never items published after it). One evidence per cluster: the
earliest item of the cluster (the original wire); ``cluster_size`` counts the outlets that
republished it, but the cluster is ONE source. Only headline, lead, date, publisher and link
are stored, never the article. Tier ``SECONDARY`` by default (the registry overrides it by
domain).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from tower.core.ports.knowledge import Evidence, SourceCapabilities, SourceQuery, Tier

if TYPE_CHECKING:
    from tower.core.db.database import Database

__all__ = ["NewsIndexSource", "search_terms"]

NAME = "news_index"
LICENSE = "headline and link only"
WINDOW = timedelta(days=30)
MAX_TERMS = 6
_WORD = re.compile(r"\w+", re.UNICODE)
_STOP_WORDS = frozenset(
    [
        "le",
        "la",
        "les",
        "un",
        "une",
        "des",
        "du",
        "de",
        "d",
        "l",
        "et",
        "ou",
        "en",
        "au",
        "aux",
        "a",
        "à",
        "est",
        "sont",
        "été",
        "être",
        "ce",
        "cet",
        "cette",
        "ces",
        "se",
        "sa",
        "son",
        "ses",
        "que",
        "qui",
        "quoi",
        "dont",
        "où",
        "ne",
        "pas",
        "plus",
        "par",
        "pour",
        "sur",
        "sous",
        "dans",
        "avec",
        "sans",
        "il",
        "elle",
        "ils",
        "elles",
        "on",
        "nous",
        "vous",
        "je",
        "tu",
        "y",
        "mais",
        "donc",
        "car",
        "ni",
        "the",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "at",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "for",
        "with",
        "without",
        "by",
        "from",
        "as",
        "an",
        "not",
        "has",
        "have",
        "had",
        "who",
        "what",
        "which",
        "where",
        "when",
        "will",
        "would",
        "can",
        "could",
        "should",
    ]
)

_SEARCH = text(
    """
    WITH hits AS (
        SELECT n.cluster_id, max(ts_rank(n.tsv, q.query)) AS rank
        FROM news_index n, websearch_to_tsquery('simple', :q) AS q(query)
        WHERE n.tsv @@ q.query AND n.cluster_id IS NOT NULL
          AND n.published_at >= :since AND n.published_at <= :until
          AND (CAST(:country AS text) IS NULL OR n.country = :country)
        GROUP BY n.cluster_id
    )
    SELECT o.id, o.url, o.publisher, o.title, o.summary, o.lang, o.published_at,
           o.origin, o.cluster_id, h.rank, s.size
    FROM hits h
    CROSS JOIN LATERAL (
        SELECT * FROM news_index n2 WHERE n2.cluster_id = h.cluster_id
        ORDER BY n2.published_at, n2.id LIMIT 1
    ) o
    CROSS JOIN LATERAL (
        SELECT count(DISTINCT n3.domain) AS size FROM news_index n3
        WHERE n3.cluster_id = h.cluster_id
    ) s
    ORDER BY h.rank DESC, o.published_at DESC
    LIMIT :limit
    """
)


def search_terms(query: str) -> str:
    """``a OR b OR c`` for ``websearch_to_tsquery``: the 6 most specific (longest) words
    that are not French/English stop words. Empty string when nothing is left."""
    seen: dict[str, None] = {}
    for word in _WORD.findall(query.lower()):
        if len(word) >= 3 and word not in _STOP_WORDS and not word.isdigit():
            seen.setdefault(word, None)
    best = sorted(seen, key=lambda w: -len(w))[:MAX_TERMS]
    return " OR ".join(best)


class NewsIndexSource:
    name = NAME
    capabilities = SourceCapabilities()
    reliability_tier = Tier.SECONDARY
    cost_per_call_usd_micros = 0
    license = LICENSE

    def __init__(self, db: Database) -> None:
        self._db = db

    async def lookup(self, query: SourceQuery) -> Sequence[Evidence]:
        terms = search_terms(query.text)
        if not terms or query.limit <= 0:
            return []
        until = query.as_of or datetime.now(UTC)
        if until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
        countries = [query.jurisdiction, None] if query.jurisdiction else [None]
        found: dict[int, Evidence] = {}
        async with self._db.session() as session:
            for country in countries:
                rows = (
                    await session.execute(
                        _SEARCH,
                        {
                            "q": terms,
                            "since": until - WINDOW,
                            "until": until,
                            "country": country,
                            "limit": query.limit,
                        },
                    )
                ).all()
                for row in rows:
                    found.setdefault(int(row.cluster_id), _evidence(row))
                if len(found) >= query.limit:
                    break
        return list(found.values())[: query.limit]


def _evidence(row: Any) -> Evidence:
    published: datetime = row.published_at
    lead = f" {row.summary}" if row.summary else ""
    body = f"{row.publisher}, {published.date().isoformat()}: {row.title.rstrip('. ')}.{lead}"
    return Evidence(
        source=NAME,
        stable_id=f"news:{row.id}",
        url=row.url,
        text=body,
        lang=row.lang,
        license=LICENSE,
        sha256=hashlib.sha256(body.encode()).hexdigest(),
        published_at=published,
        extra={
            "cluster_id": str(row.cluster_id),
            "cluster_size": str(int(row.size)),
            "origin": row.origin,
        },
        title=row.title,
        publisher=row.publisher,
        tier=Tier.SECONDARY,
    )
