"""LEGI mirror synchronization: DILA open-data archives applied in stream.

Source: ``https://echanges.dila.gouv.fr/OPENDATA/LEGI/`` (Licence Ouverte 2.0).

Verified (2026-11-14), the directory listing and a real daily diff:
- The listing is an Apache index with ``Freemium_legi_global_YYYYMMDD-HHMMSS.tar.gz``
  (full dump, about 1.2 GB) and daily ``LEGI_YYYYMMDD-HHMMSS.tar.gz`` diffs (1 to 7 MB).
- Archives are GNU tar (``ustar  `` magic, ``L`` long-name members up to ~175 chars) inside
  gzip. A diff has a root directory named after its stamp and a
  ``liste_suppression_legi.dat`` (one path per line, ending with the deleted ``LEGIARTI``
  id, no extension). Articles live under
  ``…/code_en_vigueur/LEGI/TEXT/…/LEGITEXT000006070719/article/LEGI/ARTI/…/LEGIARTI….xml``.
- Article XML: ``ARTICLE/META/META_COMMUN/ID``, ``META_SPEC/META_ARTICLE/{NUM,ETAT,
  DATE_DEBUT,DATE_FIN}`` (``2999-01-01`` = open end), ``CONTEXTE/TEXTE/@cid`` with
  ``TITRE_TXT`` and a nested ``TM/TITRE_TM`` chain, ``BLOC_TEXTUEL/CONTENU`` (HTML-ish).

Memory: gzip is inflated in bounded slices, the tar is read incrementally (512-byte
headers parsed here, no ``tarfile`` random access), non-selected members are discarded
byte by byte, and only one selected member plus one DB batch is ever held.
"""

from __future__ import annotations

import hashlib
import re
import time
import zlib
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, or_, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from tower.core.adapters.knowledge.safe_xml import UnsafeXml, parse_document, text_of
from tower.core.db.database import Database
from tower.core.domain import error_codes as ec
from tower.core.domain.factcheck.atlas import LEGI_PRIORITY_TEXTS, legi_date
from tower.core.errors import capture
from tower.core.logging import get_logger
from tower.core.models.atlas import AtlasSyncState, LegalArticle

if TYPE_CHECKING:
    from tower.core.config import Settings

__all__ = [
    "Archive",
    "ArchiveTooLarge",
    "SyncReport",
    "SyncStats",
    "TarMembers",
    "TruncatedArchive",
    "apply_archive",
    "parse_article",
    "parse_listing",
    "plan_sync",
    "sync_legi",
]

log = get_logger(__name__)

SOURCE = "legi"
STATE_KEY = "legi"
BATCH = 500
MAX_ARTICLE_BYTES = 2_000_000
MAX_DELETION_LIST_BYTES = 64_000_000
_INFLATE_SLICE = 1 << 20

_ARCHIVE_NAME = re.compile(r"(Freemium_legi_global|LEGI)_(\d{8}-\d{6})\.tar\.gz")
_ARTICLE_ID = re.compile(r"LEGIARTI\d{12}")
_DELETION_LIST = "liste_suppression_legi.dat"


class ArchiveTooLarge(ValueError):
    """The compressed archive exceeds the configured cap."""


class TruncatedArchive(ValueError):
    """The gzip stream ended early or is corrupt: the archive must not be marked applied."""


# --- listing and plan ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Archive:
    name: str
    kind: str
    """``global`` (full dump) or ``diff`` (daily)."""
    stamp: str
    """``YYYYMMDD-HHMMSS``: sorts chronologically as text."""


def archive_from_name(name: str) -> Archive | None:
    match = _ARCHIVE_NAME.fullmatch(name)
    if match is None:
        return None
    return Archive(name, "global" if match.group(1).startswith("Freemium") else "diff", match[2])


def parse_listing(html: str) -> list[Archive]:
    """Archives found in the DILA directory index, oldest first (duplicates dropped)."""
    found: dict[str, Archive] = {}
    for href in re.findall(r'href="([^"?]+)"', html):
        archive = archive_from_name(href.rsplit("/", 1)[-1])
        if archive is not None:
            found[archive.name] = archive
    return sorted(found.values(), key=lambda a: (a.stamp, a.kind))


def plan_sync(archives: list[Archive], last_applied: str | None) -> list[Archive]:
    """Archives to apply, in order.

    No state: the latest global dump then every diff after it. With state: only the diffs
    newer than ``last_applied`` (an archive name or a bare stamp).
    """
    ordered = sorted(archives, key=lambda a: (a.stamp, a.kind))
    if last_applied:
        known = archive_from_name(last_applied)
        stamp = known.stamp if known is not None else last_applied
        return [a for a in ordered if a.kind == "diff" and a.stamp > stamp]
    globals_ = [a for a in ordered if a.kind == "global"]
    if not globals_:
        return []
    base = globals_[-1]
    return [base, *(a for a in ordered if a.kind == "diff" and a.stamp > base.stamp)]


# --- incremental tar reader ---------------------------------------------------------------


@dataclass(slots=True)
class Member:
    name: str
    data: bytes | None
    """``None`` when the member was selected but skipped (too large)."""
    size: int


def _octal(raw: bytes) -> int:
    if raw and raw[0] & 0x80:  # GNU base-256 for huge sizes
        return int.from_bytes(raw, "big") & ((1 << (8 * len(raw) - 1)) - 1)
    digits = raw.split(b"\0", 1)[0].strip()
    return int(digits, 8) if digits else 0


def _cstr(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("utf-8", "replace")


class TarMembers:
    """Feed decompressed tar bytes; get the selected regular members as they complete.

    ``cap(name)`` returns the maximum size to capture for a member, or ``None`` to
    discard it. Supports ustar prefix, GNU ``L`` long names and pax ``x`` ``path``.
    """

    def __init__(self, cap: Callable[[str], int | None]) -> None:
        self._cap = cap
        self._buf = bytearray()
        self._state = "header"
        self._remaining = 0
        self._pad = 0
        self._kind = "discard"  # discard | capture | longname | pax
        self._keep = bytearray()
        self._name = ""
        self._size = 0
        self._pending_name: str | None = None
        self.members = 0

    def feed(self, data: bytes) -> list[Member]:
        out: list[Member] = []
        self._buf += data
        buf = self._buf
        n = len(buf)
        pos = 0
        while True:
            if self._state == "header":
                if n - pos < 512:
                    break
                block = bytes(buf[pos : pos + 512])
                pos += 512
                self._start(block, out)
            elif self._state == "body":
                take = min(self._remaining, n - pos)
                if take == 0:
                    break
                if self._kind != "discard":
                    self._keep += buf[pos : pos + take]
                pos += take
                self._remaining -= take
                if self._remaining == 0:
                    self._end_body(out)
            else:  # pad
                take = min(self._pad, n - pos)
                if take == 0:
                    break
                pos += take
                self._pad -= take
                if self._pad == 0:
                    self._state = "header"
        del buf[:pos]
        return out

    def _start(self, block: bytes, out: list[Member]) -> None:
        if not any(block):
            return  # end-of-archive padding
        name = _cstr(block[0:100])
        if block[257:262] == b"ustar":
            prefix = _cstr(block[345:500])
            if prefix:
                name = f"{prefix}/{name}"
        size = _octal(block[124:136])
        flag = block[156:157]
        self._pad = (-size) % 512
        self._size = size
        self._keep = bytearray()
        self._kind = "discard"
        if flag in (b"L", b"x"):
            if size > 65_536:
                self._kind = "discard"
                self._pending_name = None
            else:
                self._kind = "longname" if flag == b"L" else "pax"
        elif flag in (b"0", b"\0", b"7"):
            name = self._pending_name or name
            self._pending_name = None
            self.members += 1
            limit = self._cap(name)
            if limit is not None:
                if size <= limit:
                    self._kind = "capture"
                    self._name = name
                else:
                    out.append(Member(name, None, size))
        else:
            self._pending_name = None
        self._remaining = size
        if size == 0:
            self._end_body(out)
        else:
            self._state = "body"

    def _end_body(self, out: list[Member]) -> None:
        if self._kind == "capture":
            out.append(Member(self._name, bytes(self._keep), self._size))
        elif self._kind == "longname":
            self._pending_name = _cstr(bytes(self._keep))
        elif self._kind == "pax":
            self._pending_name = _pax_path(bytes(self._keep)) or self._pending_name
        self._keep = bytearray()
        self._kind = "discard"
        self._state = "pad" if self._pad else "header"


def _pax_path(data: bytes) -> str | None:
    pos = 0
    while pos < len(data):
        space = data.find(b" ", pos)
        if space < 0:
            return None
        try:
            length = int(data[pos:space])
        except ValueError:
            return None
        record = data[space + 1 : pos + length].rstrip(b"\n")
        key, _, value = record.partition(b"=")
        if key == b"path":
            return value.decode("utf-8", "replace")
        if length <= 0:
            return None
        pos += length
    return None


# --- article parsing ----------------------------------------------------------------------


def _child_text(root: Any, path: str) -> str:
    return text_of(root.find(path))


def parse_article(data: bytes, *, text_ids: frozenset[str], path: str) -> dict[str, Any] | None:
    """One LEGI ``ARTICLE`` file → row of ``legal_articles`` (``None`` if unusable).

    Raises :class:`UnsafeXml` for a rejected document.
    """
    elements = list(parse_document(data, select={"ARTICLE"}, max_bytes=MAX_ARTICLE_BYTES + 1024))
    if not elements:
        return None
    article = elements[0]
    article_id = _child_text(article, "META/META_COMMUN/ID")
    if not _ARTICLE_ID.fullmatch(article_id):
        return None
    texte = article.find("CONTEXTE/TEXTE")
    cid = (texte.get("cid") if texte is not None else None) or ""
    segments = set(path.split("/"))
    text_id = cid if cid in text_ids else next(iter(text_ids & segments), cid)
    if not text_id:
        return None
    body = text_of(article.find("BLOC_TEXTUEL/CONTENU"))
    if not body:
        return None
    meta = "META/META_SPEC/META_ARTICLE/"
    valid_from = legi_date(_child_text(article, meta + "DATE_DEBUT"))
    valid_to = legi_date(_child_text(article, meta + "DATE_FIN"))
    if valid_from is not None and valid_to is not None and valid_from > valid_to:
        return None
    title = text_of(texte.find("TITRE_TXT")) if texte is not None else ""
    sections = [text_of(t) for t in texte.iter("TITRE_TM")] if texte is not None else []
    nature = (texte.get("nature") if texte is not None else "") or ""
    prefix = "codes/article_lc" if nature == "CODE" else "loda/article_lc"
    return {
        "jurisdiction": "FR",
        "source": SOURCE,
        "article_id": article_id,
        "text_id": text_id,
        "text_title": title or LEGI_PRIORITY_TEXTS.get(text_id, text_id),
        "number": _child_text(article, meta + "NUM") or None,
        "status": _child_text(article, meta + "ETAT") or "UNKNOWN",
        "valid_from": valid_from,
        "valid_to": valid_to,
        "section_path": " > ".join(s for s in sections if s) or None,
        "body": body,
        "url": f"https://www.legifrance.gouv.fr/{prefix}/{article_id}",
        "sha256": hashlib.sha256(body.encode()).digest(),
    }


# --- apply --------------------------------------------------------------------------------


@dataclass(slots=True)
class SyncStats:
    members: int = 0
    upserted: int = 0
    unchanged: int = 0
    deleted: int = 0
    skipped: int = 0
    unsafe: int = 0
    bytes: int = 0
    """Compressed bytes read."""

    def add(self, other: SyncStats) -> None:
        self.members += other.members
        self.upserted += other.upserted
        self.unchanged += other.unchanged
        self.deleted += other.deleted
        self.skipped += other.skipped
        self.unsafe += other.unsafe
        self.bytes += other.bytes


async def _flush(db: Database, rows: dict[str, dict[str, Any]], stats: SyncStats) -> None:
    if not rows:
        return
    batch = list(rows.values())
    rows.clear()
    stmt = pg_insert(LegalArticle).values(batch)
    stmt = stmt.on_conflict_do_update(
        index_elements=["source", "article_id"],
        set_={
            "text_id": stmt.excluded.text_id,
            "text_title": stmt.excluded.text_title,
            "number": stmt.excluded.number,
            "status": stmt.excluded.status,
            "valid_from": stmt.excluded.valid_from,
            "valid_to": stmt.excluded.valid_to,
            "section_path": stmt.excluded.section_path,
            "body": stmt.excluded.body,
            "url": stmt.excluded.url,
            "sha256": stmt.excluded.sha256,
            "synced_at": text("now()"),
        },
        where=or_(
            *(
                getattr(LegalArticle, col).is_distinct_from(getattr(stmt.excluded, col))
                for col in (
                    "sha256",
                    "status",
                    "valid_from",
                    "valid_to",
                    "section_path",
                    "text_title",
                    "number",
                )
            )
        ),
    )
    async with db.transaction() as session:
        result = await session.execute(stmt)
    changed = int(result.rowcount or 0)  # type: ignore[attr-defined]
    stats.upserted += changed
    stats.unchanged += len(batch) - changed


async def _delete(db: Database, ids: list[str], stats: SyncStats) -> None:
    if not ids:
        return
    async with db.transaction() as session:
        result = await session.execute(
            delete(LegalArticle).where(
                LegalArticle.source == SOURCE, LegalArticle.article_id.in_(ids)
            )
        )
    stats.deleted += int(result.rowcount or 0)  # type: ignore[attr-defined]


def _deleted_ids(data: bytes) -> Iterator[str]:
    for line in data.decode("utf-8", "replace").splitlines():
        last = line.strip().rsplit("/", 1)[-1].removesuffix(".xml")
        if _ARTICLE_ID.fullmatch(last):
            yield last


async def apply_archive(
    db: Database,
    stream: AsyncIterator[bytes],
    *,
    texts: frozenset[str],
    archive_name: str,
    max_bytes: int,
) -> SyncStats:
    """Apply one archive (``.tar.gz`` byte stream) to ``legal_articles``.

    Raises :class:`ArchiveTooLarge` past ``max_bytes`` of compressed input and
    :class:`TruncatedArchive` if the gzip stream is incomplete or corrupt.
    """
    stats = SyncStats()

    def cap(name: str) -> int | None:
        if name.endswith(_DELETION_LIST):
            return MAX_DELETION_LIST_BYTES
        if "/article/" in name and name.endswith(".xml") and texts & set(name.split("/")):
            return MAX_ARTICLE_BYTES
        return None

    tar = TarMembers(cap)
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    pending: dict[str, dict[str, Any]] = {}
    delete_batch: list[str] = []
    first_unsafe: UnsafeXml | None = None

    async def handle(member: Member) -> None:
        nonlocal first_unsafe
        if member.data is None:
            stats.skipped += 1
            return
        if member.name.endswith(_DELETION_LIST):
            for article_id in _deleted_ids(member.data):
                delete_batch.append(article_id)
                if len(delete_batch) >= BATCH:
                    await _delete(db, delete_batch[:], stats)
                    delete_batch.clear()
            return
        try:
            row = parse_article(member.data, text_ids=texts, path=member.name)
        except UnsafeXml as exc:
            stats.unsafe += 1
            stats.skipped += 1
            first_unsafe = first_unsafe or exc
            return
        if row is None:
            stats.skipped += 1
            return
        pending[row["article_id"]] = row
        if len(pending) >= BATCH:
            await _flush(db, pending, stats)

    async def inflate(data: bytes) -> None:
        while data:
            try:
                chunk = inflater.decompress(data, _INFLATE_SLICE)
            except zlib.error as exc:
                raise TruncatedArchive(f"{archive_name}: corrupt gzip stream") from exc
            for member in tar.feed(chunk):
                await handle(member)
            data = inflater.unconsumed_tail
            if inflater.eof:
                break

    async for raw in stream:
        stats.bytes += len(raw)
        if stats.bytes > max_bytes:
            raise ArchiveTooLarge(f"{archive_name}: more than {max_bytes} bytes")
        await inflate(raw)
    if not inflater.eof:
        raise TruncatedArchive(f"{archive_name}: gzip stream ended early")
    await _flush(db, pending, stats)
    await _delete(db, delete_batch, stats)
    stats.members = tar.members
    if first_unsafe is not None:
        capture(
            ec.ATL_UNSAFE_XML,
            first_unsafe,
            archive=archive_name,
            rejected_members=stats.unsafe,
        )
    return stats


# --- orchestration ------------------------------------------------------------------------

StreamFactory = Callable[[str], AsyncIterator[bytes]]


@dataclass(slots=True)
class SyncReport:
    archives: list[str] = field(default_factory=list)
    stats: SyncStats = field(default_factory=SyncStats)
    duration_s: float = 0.0
    up_to_date: bool = False
    needs_bootstrap: bool = False
    """``diffs_only`` and no archive applied yet: the global dump must be imported first
    (``tower atlas legi-sync``, it does not fit in a job lease)."""


async def _read_state(db: Database) -> dict[str, Any]:
    async with db.session() as session:
        row = await session.get(AtlasSyncState, STATE_KEY)
    return dict(row.state) if row is not None else {}


async def _write_state(db: Database, state: dict[str, Any]) -> None:
    stmt = pg_insert(AtlasSyncState).values(key=STATE_KEY, state=state)
    stmt = stmt.on_conflict_do_update(
        index_elements=["key"], set_={"state": stmt.excluded.state, "updated_at": text("now()")}
    )
    async with db.transaction() as session:
        await session.execute(stmt)


def selected_texts(settings: Settings) -> frozenset[str]:
    configured = {t.strip() for t in settings.atlas_legi_texts.split(",") if t.strip()}
    return frozenset(configured or LEGI_PRIORITY_TEXTS)


async def sync_legi(
    db: Database,
    settings: Settings,
    *,
    http_stream: StreamFactory | None = None,
    transport: Any = None,
    diffs_only: bool = False,
    max_archives: int | None = None,
) -> SyncReport:
    """Bring the mirror up to date; the state is saved after each archive (resumable).

    ``diffs_only`` (the daily job): never download the global dump; without a state the
    report says ``needs_bootstrap``. ``max_archives`` bounds one run (the next run
    resumes from the state).

    ``transport`` (httpx) serves the listing and, without ``http_stream``, the archives.
    """
    import httpx

    started = time.monotonic()
    base = settings.atlas_legi_base_url.rstrip("/") + "/"
    max_bytes = settings.atlas_legi_max_archive_mb * 1_000_000
    texts = selected_texts(settings)
    report = SyncReport()
    timeout = httpx.Timeout(settings.atlas_http_timeout_s, read=120.0)
    async with httpx.AsyncClient(
        transport=transport,
        timeout=timeout,
        headers={"User-Agent": settings.http_user_agent, "Accept-Encoding": "identity"},
        follow_redirects=True,
        trust_env=transport is None,
    ) as client:

        async def default_stream(url: str) -> AsyncIterator[bytes]:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                declared = int(response.headers.get("content-length") or 0)
                if declared > max_bytes:
                    raise ArchiveTooLarge(f"{url}: {declared} bytes exceeds the cap")
                async for chunk in response.aiter_bytes(65_536):
                    yield chunk

        listing = await client.get(base)
        listing.raise_for_status()
        state = await _read_state(db)
        plan = plan_sync(parse_listing(listing.text), state.get("last_applied"))
        if diffs_only and not state.get("last_applied"):
            report.needs_bootstrap = True
            plan = []
        if max_archives is not None:
            plan = plan[:max_archives]
        if not plan and not report.needs_bootstrap:
            report.up_to_date = True
        for archive in plan:
            stream = (http_stream or default_stream)(base + archive.name)
            stats = await apply_archive(
                db, stream, texts=texts, archive_name=archive.name, max_bytes=max_bytes
            )
            report.archives.append(archive.name)
            report.stats.add(stats)
            state = {
                "last_applied": archive.name,
                "articles": int(state.get("articles", 0)) + stats.upserted - stats.deleted,
                "applied_at": datetime.now(UTC).isoformat(),
                "last_stats": {
                    "members": stats.members,
                    "upserted": stats.upserted,
                    "unchanged": stats.unchanged,
                    "deleted": stats.deleted,
                    "skipped": stats.skipped,
                },
            }
            await _write_state(db, state)
    report.duration_s = round(time.monotonic() - started, 2)
    return report
