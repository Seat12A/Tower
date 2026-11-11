"""Ingestion of observed content (E0 normalisation, dedup, observations) and filters.

One batch = one transaction. The installation token is only used to derive the
pseudonym of the day; it is never stored, logged or put in an error.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from sqlalchemy import Boolean, case, func, literal_column, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from tower.core.domain import error_codes as ec
from tower.core.domain.errors import DomainError, InvalidInput
from tower.core.domain.ingestion import (
    MAX_MEDIA,
    MAX_TEXT_CHARS,
    IngestResult,
    ItemInput,
    as_utc,
    clamp_observed_at,
    validate_filters,
    validate_media,
)
from tower.core.domain.language import detect_language
from tower.core.domain.platforms import Platform, parse_platform, validate_external_id
from tower.core.domain.privacy import installation_pseudonym, validate_installation_token
from tower.core.domain.security import Actor, ActorKind, Permission
from tower.core.domain.text import (
    content_hash,
    normalize,
    simhash64,
    useful_word_count,
)
from tower.core.errors import capture
from tower.core.events.outbox import publish
from tower.core.jobs.queue import enqueue
from tower.core.models.ingestion import (
    ContentItem,
    ContentMedia,
    ExtensionFilterVersion,
    MediaAsset,
    Observation,
    PlatformAccount,
)
from tower.core.usecases.installations import daily_secret

EXTENSION_SOURCE = "extension"
SERVICE_SOURCES = frozenset({"radar", "feed"})
INGEST_QUEUE = "ingest"
_MAX_HANDLE = 100
_MAX_HINT = 32

_QUOTA = text(
    """
    INSERT INTO installation_daily (day, pseudonym, items, quota)
    VALUES (:day, :pseudonym, :n, :quota)
    ON CONFLICT (day, pseudonym) DO UPDATE
        SET items = installation_daily.items + EXCLUDED.items
        WHERE installation_daily.items + EXCLUDED.items <= EXCLUDED.quota
    RETURNING items
    """
)


class _Prepared:
    """A valid item with its E0 fingerprints."""

    __slots__ = (
        "author_key",
        "handle",
        "hash",
        "hint",
        "index",
        "item",
        "key",
        "lang",
        "lang_confidence",
        "norm_words",
        "observed_at",
        "platform",
        "simhash",
    )

    def __init__(self, index: int, item: ItemInput, platform: Platform, now: datetime) -> None:
        self.index = index
        self.item = item
        self.platform = platform
        self.key = (platform.value, item.external_id)
        normalized = normalize(item.text)
        self.hash = content_hash(normalized)
        self.simhash = simhash64(normalized)
        self.norm_words = useful_word_count(item.text)
        self.lang, self.lang_confidence = detect_language(item.text, item.lang_hint)
        self.hint = item.lang_hint[:_MAX_HINT] if item.lang_hint else None
        self.observed_at = clamp_observed_at(item.observed_at, now)
        author = item.author
        self.author_key: tuple[str, str] | None = None
        self.handle: str | None = None
        if author is not None:
            if author.external_id is not None:
                self.author_key = (platform.value, author.external_id)
            if author.handle:
                handle = author.handle.strip().lstrip("@")
                self.handle = handle[:_MAX_HANDLE] or None


def _validate_item(item: ItemInput, now: datetime) -> _Prepared:
    platform = parse_platform(item.platform)
    validate_external_id(platform, item.external_id)
    if item.author is not None and item.author.external_id is not None:
        validate_external_id(platform, item.author.external_id)
    for ref in (item.parent_external_id, item.quoted_external_id):
        if ref is not None:
            validate_external_id(platform, ref)
    if len(item.text) > MAX_TEXT_CHARS:
        raise InvalidInput("Text too long", max_chars=MAX_TEXT_CHARS)
    if len(item.media) > MAX_MEDIA:
        raise InvalidInput("Too many media", max_media=MAX_MEDIA)
    for media in item.media:
        validate_media(media)
    return _Prepared(0, item, platform, now)


def _is_missing_partition(exc: DBAPIError) -> bool:
    return "no partition of relation" in str(exc.orig) or "no partition of relation" in str(exc)


async def _consume_quota(
    session: AsyncSession, token: str, count: int, quota: int, now: datetime
) -> bytes:
    """Pseudonym of the day; raises ``ING_QUOTA_EXCEEDED`` if the batch does not fit."""
    today = now.date()
    pseudonym = installation_pseudonym(await daily_secret(session, today), token)
    if count == 0:
        return pseudonym
    used = None
    if count <= quota:
        used = (
            await session.execute(
                _QUOTA, {"day": today, "pseudonym": pseudonym, "n": count, "quota": quota}
            )
        ).scalar_one_or_none()
    if used is None:
        raise InvalidInput(
            "Daily ingestion quota exceeded", code=ec.ING_QUOTA_EXCEEDED, quota=quota
        )
    return pseudonym


async def _upsert_accounts(
    session: AsyncSession, prepared: Sequence[_Prepared]
) -> dict[tuple[str, str], int]:
    handles: dict[tuple[str, str], str | None] = {}
    for p in prepared:
        if p.author_key is not None:
            handles[p.author_key] = p.handle or handles.get(p.author_key)
    if not handles:
        return {}
    rows = [{"platform": k[0], "external_id": k[1], "handle": handles[k]} for k in sorted(handles)]
    ins = insert(PlatformAccount).values(rows)
    stmt = ins.on_conflict_do_update(
        index_elements=[PlatformAccount.platform, PlatformAccount.external_id],
        set_={
            "handle": func.coalesce(ins.excluded.handle, PlatformAccount.handle),
            "updated_at": func.now(),
        },
    ).returning(PlatformAccount.id, PlatformAccount.platform, PlatformAccount.external_id)
    result = await session.execute(stmt)
    return {(r.platform, r.external_id): r.id for r in result}


async def _upsert_contents(
    session: AsyncSession,
    prepared: Sequence[_Prepared],
    accounts: dict[tuple[str, str], int],
    now: datetime,
) -> list[Any]:
    """One multi-row upsert. In-batch duplicates are merged (one row per content)."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for p in prepared:
        item = p.item
        metrics = dict(item.metrics)
        row = merged.get(p.key)
        if row is None:
            merged[p.key] = {
                "platform": p.key[0],
                "external_id": p.key[1],
                "author_account_id": accounts.get(p.author_key) if p.author_key else None,
                "text": item.text,
                "lang": p.lang,
                "lang_confidence": p.lang_confidence,
                "lang_hint": p.hint,
                "useful_words": p.norm_words,
                "content_hash": p.hash,
                "simhash": p.simhash,
                "parent_external_id": item.parent_external_id,
                "quoted_external_id": item.quoted_external_id,
                "metrics": metrics,
                "metrics_at": now if metrics else None,
                "created_at_platform": as_utc(item.created_at) if item.created_at else None,
                "observation_count": 1,
            }
        else:
            row["observation_count"] += 1
            if metrics:
                row["metrics"] = metrics
                row["metrics_at"] = now
    rows = [merged[k] for k in sorted(merged)]
    ins = insert(ContentItem).values(rows)
    has_metrics = ins.excluded.metrics != literal_column("'{}'::jsonb")
    stmt = ins.on_conflict_do_update(
        index_elements=[ContentItem.platform, ContentItem.external_id],
        set_={
            "last_seen_at": func.now(),
            "observation_count": ContentItem.observation_count + ins.excluded.observation_count,
            "metrics": case((has_metrics, ins.excluded.metrics), else_=ContentItem.metrics),
            "metrics_at": case(
                (has_metrics, ins.excluded.metrics_at), else_=ContentItem.metrics_at
            ),
        },
    ).returning(
        ContentItem.id,
        ContentItem.public_id,
        ContentItem.platform,
        ContentItem.external_id,
        ContentItem.lang,
        literal_column("(xmax = 0)", Boolean).label("inserted"),
    )
    return list((await session.execute(stmt)).all())


async def _link_media(
    session: AsyncSession, prepared: Sequence[_Prepared], content_ids: dict[tuple[str, str], int]
) -> None:
    urls: dict[bytes, tuple[str, str]] = {}
    links: list[tuple[tuple[str, str], bytes, int]] = []
    for p in prepared:
        for position, media in enumerate(p.item.media):
            digest = sha256(media.url.encode()).digest()
            urls.setdefault(digest, (media.kind, media.url))
            links.append((p.key, digest, position))
    if not urls:
        return
    rows = [
        {"kind": kind, "url": url, "url_sha256": digest}
        for digest, (kind, url) in sorted(urls.items())
    ]
    ins = insert(MediaAsset).values(rows)
    stmt = ins.on_conflict_do_update(
        index_elements=[MediaAsset.url_sha256], set_={"url": MediaAsset.url}
    ).returning(MediaAsset.id, MediaAsset.url_sha256)
    media_ids = {bytes(r.url_sha256): r.id for r in await session.execute(stmt)}
    link_rows = {(content_ids[key], media_ids[digest]): position for key, digest, position in links}
    link_stmt = insert(ContentMedia).values(
        [
            {"content_id": cid, "media_id": mid, "position": pos}
            for (cid, mid), pos in sorted(link_rows.items())
        ]
    )
    await session.execute(
        link_stmt.on_conflict_do_nothing(
            index_elements=[ContentMedia.content_id, ContentMedia.media_id]
        )
    )


async def ingest_items(
    session: AsyncSession,
    actor: Actor,
    items: Sequence[ItemInput],
    *,
    source: str,
    filter_version: str | None,
    installation_token: str | None,
    max_items: int,
    daily_quota: int,
    now: datetime | None = None,
) -> IngestResult:
    """Ingest a batch of observed items (see docs/ingestion.md)."""
    actor.require(Permission.CONTENT_INGEST)
    now = as_utc(now) if now is not None else datetime.now(UTC)
    is_installation = actor.kind is ActorKind.INSTALLATION
    if is_installation:
        source = EXTENSION_SOURCE
    elif source not in SERVICE_SOURCES:
        raise InvalidInput("Invalid observation source", source=str(source)[:32])
    if len(items) > max_items:
        raise InvalidInput(
            "Too many items in the batch",
            code=ec.ING_BATCH_TOO_LARGE,
            max_items=max_items,
            received=len(items),
        )
    token = validate_installation_token(installation_token) if is_installation else None

    prepared: list[_Prepared] = []
    errors: list[tuple[int, str]] = []
    for index, item in enumerate(items):
        try:
            p = _validate_item(item, now)
        except DomainError as exc:
            errors.append((index, exc.code))
            continue
        p.index = index
        prepared.append(p)
    accepted = len(prepared)
    if accepted == 0:
        return IngestResult(0, 0, 0, len(errors), tuple(errors))

    pseudonym = None
    if token is not None:
        pseudonym = await _consume_quota(session, token, accepted, daily_quota, now)

    accounts = await _upsert_accounts(session, prepared)
    rows = await _upsert_contents(session, prepared, accounts, now)
    content_ids = {(r.platform, r.external_id): r.id for r in rows}
    await _link_media(session, prepared, content_ids)

    observations = [
        {
            "observed_at": p.observed_at,
            "content_id": content_ids[p.key],
            "platform": p.key[0],
            "source": source,
            "installation_pseudonym": pseudonym,
            "filter_version": filter_version[:64] if filter_version else None,
        }
        for p in prepared
    ]
    try:
        await session.execute(insert(Observation), observations)
    except DBAPIError as exc:
        if not _is_missing_partition(exc):
            raise
        capture(ec.ING_PARTITION_MISSING, exc)
        raise DomainError("No observations partition", code=ec.ING_PARTITION_MISSING) from exc

    new_rows = sorted((r for r in rows if r.inserted), key=lambda r: r.id)
    for r in new_rows:
        await publish(
            session,
            "content.ingested",
            {
                "id": str(r.id),
                "public_id": str(r.public_id),
                "platform": r.platform,
                "external_id": r.external_id,
                "lang": r.lang,
                "source": source,
            },
            aggregate_type="content",
            aggregate_id=str(r.public_id),
            actor=actor.label,
        )
        await enqueue(
            session,
            INGEST_QUEUE,
            "ingest.link_copies",
            {"content_id": r.id},
            dedupe_key=f"copies:{r.id}",
        )
    return IngestResult(
        accepted=accepted,
        new=len(new_rows),
        duplicates=accepted - len(new_rows),
        rejected=len(errors),
        errors=tuple(errors),
    )


# --- Extension filters ------------------------------------------------------------------


async def get_active_filters(session: AsyncSession) -> tuple[str | None, dict[str, Any]]:
    row = (
        await session.execute(
            select(ExtensionFilterVersion.version, ExtensionFilterVersion.filters).where(
                ExtensionFilterVersion.active
            )
        )
    ).first()
    if row is None:
        return None, {"keywords": [], "accounts": [], "thresholds": {}}
    return row.version, dict(row.filters)


async def publish_filters(
    session: AsyncSession, actor: Actor, *, version: str, filters: object
) -> ExtensionFilterVersion:
    actor.require(Permission.STAFF_MANAGE)
    clean = validate_filters(filters)
    version = version.strip()
    if not 1 <= len(version) <= 64:
        raise InvalidInput("Filter version must be 1 to 64 characters")
    exists = await session.scalar(
        select(ExtensionFilterVersion.id).where(ExtensionFilterVersion.version == version)
    )
    if exists is not None:
        raise InvalidInput("Filter version already exists", version=version)
    await session.execute(
        update(ExtensionFilterVersion)
        .where(ExtensionFilterVersion.active)
        .values(active=False)
        .execution_options(synchronize_session=False)
    )
    row = ExtensionFilterVersion(
        version=version, filters=clean, active=True, created_by=actor.label
    )
    session.add(row)
    await session.flush()
    await publish(
        session,
        "extension_filters.published",
        {"version": version},
        aggregate_type="extension_filters",
        aggregate_id=version,
        actor=actor.label,
    )
    return row
