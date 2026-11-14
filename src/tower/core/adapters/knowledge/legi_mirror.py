"""``legi_mirror``: French legal texts read from the local LEGI mirror (``legal_articles``).

Primary tier, Licence Ouverte 2.0, no HTTP and no cost. The article in force at
``query.as_of`` (default: today) is returned with its dates in force at the start of the
evidence text, so the judge always knows which version it reads. When no version is in
force at that date, the latest version is returned with its status (``ABROGE``…).

Lookup: an explicit reference (``article 432-13``, ``art. L. 1234-5``, ``article 49.3``,
optionally with a code name) is resolved exactly; otherwise a full-text search
(``websearch_to_tsquery('french')``, ranked by ``ts_rank``) is run.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import text

from tower.core.db.database import Database
from tower.core.ports.knowledge import Evidence, SourceCapabilities, SourceQuery, Tier

__all__ = ["LegiMirrorSource", "article_at", "parse_reference"]

NAME = "legi_mirror"
LICENSE = "Licence Ouverte 2.0"
BODY_CHARS = 1_500
MAX_VERSION_ROWS = 300

_CODES: tuple[tuple[str, str], ...] = (
    ("procedure penale", "LEGITEXT000006071154"),
    ("code penal", "LEGITEXT000006070719"),
    ("penal", "LEGITEXT000006070719"),
    ("code civil", "LEGITEXT000006070721"),
    ("civil", "LEGITEXT000006070721"),
    ("travail", "LEGITEXT000006072050"),
    ("electoral", "LEGITEXT000006070239"),
    ("sante publique", "LEGITEXT000006072665"),
    ("securite interieure", "LEGITEXT000025503132"),
    ("ceseda", "LEGITEXT000006070158"),
    ("etrangers", "LEGITEXT000006070158"),
    ("entree et du sejour", "LEGITEXT000006070158"),
    ("constitution", "LEGITEXT000006071194"),
)
_ARTICLE_REF = re.compile(
    r"\b(?:articles?|art\.?)\s*((?:[LRDA]\.?\s*)?\d+(?:\s*[-.]\s*\d+)*)", re.IGNORECASE
)
_WORD = re.compile(r"[a-z0-9]{3,}")
_STOP = frozenset({"les", "des", "une", "est", "sont", "pour", "dans", "que", "qui", "pas"})


def _norm(value: str) -> str:
    stripped = unicodedata.normalize("NFKD", value.lower())
    return "".join(c for c in stripped if not unicodedata.combining(c))


@dataclass(frozen=True, slots=True)
class Reference:
    candidates: tuple[str, ...]
    """Article numbers as stored in LEGI, most specific first (``49-3`` then ``49``)."""
    text_id: str | None


def parse_reference(query_text: str) -> Reference | None:
    """Article number(s) and code of an explicit reference, or ``None``."""
    match = _ARTICLE_REF.search(query_text)
    if match is None:
        return None
    raw = re.sub(r"\s+", "", match.group(1)).upper()
    letter = ""
    if raw[0] in "LRDA" and not raw[0].isdigit():
        letter, raw = raw[0], raw[1:].lstrip(".")
    candidates = [letter + raw.replace(".", "-")]
    if "." in raw:  # "49.3" may be paragraph 3 of article 49
        candidates.append(letter + raw.split(".", 1)[0])
    normalized = _norm(query_text)
    text_id = next((tid for key, tid in _CODES if key in normalized), None)
    return Reference(tuple(dict.fromkeys(candidates)), text_id)


def article_at(rows: Sequence[Any], as_of: date) -> Any | None:
    """The version in force at ``as_of`` among versions of one article, else the latest.

    Rows only need ``valid_from`` and ``valid_to`` attributes.
    """
    if not rows:
        return None

    def start(row: Any) -> date:
        return row.valid_from or date.min

    in_force = [
        r
        for r in rows
        if (r.valid_from is None or r.valid_from <= as_of)
        and (r.valid_to is None or r.valid_to > as_of)
    ]
    pool = in_force or list(rows)
    return max(pool, key=start)


def _dates_phrase(valid_from: date | None, valid_to: date | None, status: str) -> str:
    if valid_from is not None and valid_to is None:
        span = f"in force from {valid_from.isoformat()}, still in force"
    elif valid_from is not None and valid_to is not None:
        span = f"in force from {valid_from.isoformat()} to {valid_to.isoformat()}"
    elif valid_to is not None:
        span = f"in force until {valid_to.isoformat()}"
    else:
        span = "dates in force unknown"
    return f"{span}; status {status}"


def _truncate(body: str) -> str:
    if len(body) <= BODY_CHARS:
        return body
    return body[:BODY_CHARS].rsplit(" ", 1)[0] + " […]"


_COLUMNS = (
    "article_id, text_id, text_title, number, status, valid_from, valid_to, section_path, body, url"
)


class LegiMirrorSource:
    name = NAME
    capabilities = SourceCapabilities(
        claim_types=frozenset({"legal_text", "procedure"}), jurisdictions=frozenset({"FR"})
    )
    reliability_tier = Tier.PRIMARY
    cost_per_call_usd_micros = 0
    license = LICENSE

    def __init__(self, db: Database) -> None:
        self._db = db

    async def lookup(self, query: SourceQuery) -> Sequence[Evidence]:
        as_of = (query.as_of or datetime.now(UTC)).date()
        limit = max(1, min(query.limit, 5))
        reference = parse_reference(query.text)
        rows: list[Any] = []
        if reference is not None:
            rows = await self._by_reference(reference, as_of, limit)
        if not rows:
            rows = await self._full_text(query.text, reference, as_of, limit)
        return [self._evidence(row, as_of) for row in rows]

    # --- queries -------------------------------------------------------------------------

    async def _by_reference(self, ref: Reference, as_of: date, limit: int) -> list[Any]:
        sql = (
            f"SELECT {_COLUMNS} FROM legal_articles "  # noqa: S608 (constant column list)
            "WHERE source = 'legi' AND number = ANY(:nums)"
        )
        params: dict[str, Any] = {"nums": list(ref.candidates), "n": MAX_VERSION_ROWS}
        if ref.text_id is not None:
            sql += " AND text_id = :tid"
            params["tid"] = ref.text_id
        sql += " ORDER BY valid_from NULLS FIRST LIMIT :n"
        async with self._db.session() as session:
            found = (await session.execute(text(sql), params)).all()
        for number in ref.candidates:  # most specific candidate that exists wins
            groups: dict[str, list[Any]] = {}
            for row in found:
                if row.number == number:
                    groups.setdefault(row.text_id, []).append(row)
            if groups:
                picked = [article_at(versions, as_of) for versions in groups.values()]
                return [r for r in picked if r is not None][:limit]
        return []

    async def _full_text(
        self, query_text: str, ref: Reference | None, as_of: date, limit: int
    ) -> list[Any]:
        words = list(dict.fromkeys(w for w in _WORD.findall(_norm(query_text)) if w not in _STOP))
        if not words:
            return []
        words = words[:12]
        tid = ref.text_id if ref is not None else None
        for joiner in (" ", " or "):
            q = joiner.join(words)
            rows = await self._search(q, tid, as_of, limit, in_force=True)
            if rows:
                return rows
        for joiner in (" ", " or "):
            rows = await self._search(joiner.join(words), tid, as_of, limit, in_force=False)
            if rows:
                return rows
        return []

    async def _search(
        self, q: str, tid: str | None, as_of: date, limit: int, *, in_force: bool
    ) -> list[Any]:
        sql = (
            f"SELECT {_COLUMNS}, ts_rank(tsv, query) AS rank "  # noqa: S608 (constant columns)
            "FROM legal_articles, websearch_to_tsquery('french', :q) AS query "
            "WHERE source = 'legi' AND tsv @@ query"
        )
        params: dict[str, Any] = {"q": q, "n": limit * 8 if not in_force else limit, "d": as_of}
        if in_force:
            sql += (
                " AND (valid_from IS NULL OR valid_from <= :d)"
                " AND (valid_to IS NULL OR valid_to > :d)"
            )
        if tid is not None:
            sql += " AND text_id = :tid"
            params["tid"] = tid
        sql += " ORDER BY rank DESC, valid_from DESC NULLS LAST LIMIT :n"
        async with self._db.session() as session:
            found = (await session.execute(text(sql), params)).all()
        if in_force:
            return list(found)
        seen: set[tuple[str, str | None]] = set()
        out: list[Any] = []
        for row in found:  # best rank first; one (latest) version per article
            key = (row.text_id, row.number)
            if key not in seen:
                seen.add(key)
                out.append(row)
        return out[:limit]

    # --- evidence ------------------------------------------------------------------------

    @staticmethod
    def _evidence(row: Any, as_of: date) -> Evidence:
        number = row.number or "?"
        in_force_at = (row.valid_from is None or row.valid_from <= as_of) and (
            row.valid_to is None or row.valid_to > as_of
        )
        head = f"{row.text_title}, article {number}"
        body = (
            f"{head} ({_dates_phrase(row.valid_from, row.valid_to, row.status)}): "
            f"{_truncate(row.body)}"
        )
        extra = {
            "text_id": row.text_id,
            "number": number,
            "valid_from": row.valid_from.isoformat() if row.valid_from else "",
            "valid_to": row.valid_to.isoformat() if row.valid_to else "",
            "status": row.status,
            "as_of": as_of.isoformat(),
            "in_force_at_as_of": "yes" if in_force_at else "no",
        }
        if row.section_path:
            extra["section_path"] = row.section_path
        published = (
            datetime(row.valid_from.year, row.valid_from.month, row.valid_from.day, tzinfo=UTC)
            if row.valid_from
            else None
        )
        return Evidence(
            source=NAME,
            stable_id=row.article_id,
            url=row.url,
            text=body,
            lang="fr",
            license=LICENSE,
            sha256=hashlib.sha256(body.encode()).hexdigest(),
            published_at=published,
            version=row.valid_from.isoformat() if row.valid_from else None,
            extra=extra,
            title=head,
            publisher="Légifrance (DILA)",
            tier=Tier.PRIMARY,
        )
