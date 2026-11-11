"""Generic multi-platform ingestion tables. See docs/ingestion.md and docs/database.md."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    Date,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import (
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from tower.core.db.base import Base
from tower.core.domain.platforms import Platform, PublicRole
from tower.core.models._helpers import check_in

#: Format of X identifiers (digits, BIGINT bound), shared by the X-aware tables.
X_ID_CHECK = (
    "platform <> 'x' OR (external_id ~ '^[0-9]{1,19}$' "
    "AND external_id::numeric <= 9223372036854775807)"
)
OBSERVATION_SOURCES = ("extension", "radar", "feed")
SNAPSHOT_REASONS = ("dossier_opened", "recheck", "manual")
MEDIA_KINDS = ("image", "video", "audio", "document")


class Entity(Base):
    """Public person or organisation. Never a political side here."""

    __tablename__ = "entities"
    __table_args__ = (
        check_in("kind", ["person", "organization"], "kind"),
        CheckConstraint(
            "public_role IS NULL OR public_role IN ("
            + ", ".join(f"'{r.value}'" for r in PublicRole)
            + ")",
            name="public_role",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, server_default=sa_text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    public_role: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PlatformAccount(Base):
    __tablename__ = "platform_accounts"
    __table_args__ = (
        UniqueConstraint("platform", "external_id"),
        check_in("platform", Platform, "platform"),
        CheckConstraint(X_ID_CHECK, name="x_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    platform: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str] = mapped_column(Text)
    handle: Mapped[str | None] = mapped_column(Text)
    entity_id: Mapped[int | None] = mapped_column(
        ForeignKey("entities.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class WatchedAccount(Base):
    """Account followed by the team (radar). Only changes surveillance and latency."""

    __tablename__ = "watched_accounts"
    __table_args__ = (
        check_in("category", PublicRole, "category"),
        CheckConstraint("priority BETWEEN -100 AND 100", name="priority"),
        Index("ix_watched_accounts_active_priority", "active", "priority"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, server_default=sa_text("gen_random_uuid()")
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("platform_accounts.id", ondelete="RESTRICT"), unique=True
    )
    category: Mapped[str] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(SmallInteger, server_default=sa_text("0"))
    since_cursor: Mapped[str | None] = mapped_column(Text)
    """Reading cursor of the radar (last native id seen)."""
    active: Mapped[bool] = mapped_column(server_default=sa_text("true"))
    note: Mapped[str | None] = mapped_column(Text)
    added_by: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(server_default=sa_text("1"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012


class WatchedAccountEvent(Base):
    """Immutable audit log of the watchlist."""

    __tablename__ = "watched_account_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    watched_account_id: Mapped[int] = mapped_column(
        ForeignKey("watched_accounts.id", ondelete="RESTRICT"), index=True
    )
    action: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(Text)
    changes: Mapped[dict[str, Any]] = mapped_column(server_default=sa_text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ContentItem(Base):
    """Observed content of any platform. Not partitioned (internal BIGINT key everywhere)."""

    __tablename__ = "content_items"
    __table_args__ = (
        UniqueConstraint("platform", "external_id"),
        check_in("platform", Platform, "platform"),
        CheckConstraint(X_ID_CHECK, name="x_id"),
        CheckConstraint("octet_length(content_hash) = 32", name="content_hash"),
        Index(
            "ix_content_items_x_id",
            sa_text("(external_id::bigint)"),
            postgresql_where=sa_text("platform = 'x'"),
        ),
        Index("ix_content_items_simhash_b0", "simhash_b0", "first_seen_at"),
        Index("ix_content_items_simhash_b1", "simhash_b1", "first_seen_at"),
        Index("ix_content_items_simhash_b2", "simhash_b2", "first_seen_at"),
        Index("ix_content_items_simhash_b3", "simhash_b3", "first_seen_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, server_default=sa_text("gen_random_uuid()")
    )
    platform: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str] = mapped_column(Text)
    author_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("platform_accounts.id", ondelete="SET NULL"), index=True
    )
    text: Mapped[str] = mapped_column(Text)
    """Original text, unchanged."""
    lang: Mapped[str] = mapped_column(Text, index=True)
    lang_confidence: Mapped[float] = mapped_column(Float)
    lang_hint: Mapped[str | None] = mapped_column(Text)
    useful_words: Mapped[int] = mapped_column(SmallInteger)
    content_hash: Mapped[bytes] = mapped_column(LargeBinary, index=True)
    simhash: Mapped[int] = mapped_column(BigInteger)
    simhash_b0: Mapped[int] = mapped_column(
        Integer, Computed("((simhash >> 0) & 65535)::integer", persisted=True)
    )
    simhash_b1: Mapped[int] = mapped_column(
        Integer, Computed("((simhash >> 16) & 65535)::integer", persisted=True)
    )
    simhash_b2: Mapped[int] = mapped_column(
        Integer, Computed("((simhash >> 32) & 65535)::integer", persisted=True)
    )
    simhash_b3: Mapped[int] = mapped_column(
        Integer, Computed("((simhash >> 48) & 65535)::integer", persisted=True)
    )
    parent_external_id: Mapped[str | None] = mapped_column(Text)
    quoted_external_id: Mapped[str | None] = mapped_column(Text)
    copy_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("content_items.id", ondelete="SET NULL"), index=True
    )
    metrics: Mapped[dict[str, Any]] = mapped_column(server_default=sa_text("'{}'::jsonb"))
    metrics_at: Mapped[datetime | None] = mapped_column()
    created_at_platform: Mapped[datetime | None] = mapped_column()
    first_seen_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    observation_count: Mapped[int] = mapped_column(Integer, server_default=sa_text("1"))


class ContentSnapshot(Base):
    __tablename__ = "content_snapshots"
    __table_args__ = (check_in("reason", SNAPSHOT_REASONS, "reason"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    content_id: Mapped[int] = mapped_column(
        ForeignKey("content_items.id", ondelete="CASCADE"), index=True
    )
    reason: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    metrics: Mapped[dict[str, Any]] = mapped_column(server_default=sa_text("'{}'::jsonb"))
    data: Mapped[dict[str, Any]] = mapped_column(server_default=sa_text("'{}'::jsonb"))
    taken_at: Mapped[datetime] = mapped_column(server_default=func.now())


class MediaAsset(Base):
    """Media seen in content. Perceptual hash computed by a later job (schema ready)."""

    __tablename__ = "media_assets"
    __table_args__ = (
        check_in("kind", MEDIA_KINDS, "kind"),
        CheckConstraint("octet_length(url_sha256) = 32", name="url_sha256"),
        Index(
            "ix_media_assets_phash_b0", "phash_b0", postgresql_where=sa_text("phash IS NOT NULL")
        ),
        Index(
            "ix_media_assets_phash_b1", "phash_b1", postgresql_where=sa_text("phash IS NOT NULL")
        ),
        Index(
            "ix_media_assets_phash_b2", "phash_b2", postgresql_where=sa_text("phash IS NOT NULL")
        ),
        Index(
            "ix_media_assets_phash_b3", "phash_b3", postgresql_where=sa_text("phash IS NOT NULL")
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    url_sha256: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    sha256: Mapped[bytes | None] = mapped_column(LargeBinary, index=True)
    phash: Mapped[int | None] = mapped_column(BigInteger)
    phash_b0: Mapped[int | None] = mapped_column(
        Integer, Computed("((phash >> 0) & 65535)::integer", persisted=True)
    )
    phash_b1: Mapped[int | None] = mapped_column(
        Integer, Computed("((phash >> 16) & 65535)::integer", persisted=True)
    )
    phash_b2: Mapped[int | None] = mapped_column(
        Integer, Computed("((phash >> 32) & 65535)::integer", persisted=True)
    )
    phash_b3: Mapped[int | None] = mapped_column(
        Integer, Computed("((phash >> 48) & 65535)::integer", persisted=True)
    )
    first_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ContentMedia(Base):
    __tablename__ = "content_media"

    content_id: Mapped[int] = mapped_column(
        ForeignKey("content_items.id", ondelete="CASCADE"), primary_key=True
    )
    media_id: Mapped[int] = mapped_column(
        ForeignKey("media_assets.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    position: Mapped[int] = mapped_column(SmallInteger, server_default=sa_text("0"))


class Observation(Base):
    """One sighting of a content. Partitioned by week on ``observed_at`` (30 days kept)."""

    __tablename__ = "observations"
    __table_args__ = (
        PrimaryKeyConstraint("id", "observed_at"),
        check_in("source", OBSERVATION_SOURCES, "source"),
        check_in("platform", Platform, "platform"),
        CheckConstraint(
            "installation_pseudonym IS NULL OR octet_length(installation_pseudonym) = 16",
            name="pseudonym",
        ),
        Index("ix_observations_content_id_observed_at", "content_id", "observed_at"),
        {"postgresql_partition_by": "RANGE (observed_at)"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity())
    observed_at: Mapped[datetime] = mapped_column()
    content_id: Mapped[int] = mapped_column(ForeignKey("content_items.id", ondelete="CASCADE"))
    platform: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    installation_pseudonym: Mapped[bytes | None] = mapped_column(LargeBinary)
    filter_version: Mapped[str | None] = mapped_column(Text)


class ObservationCounter(Base):
    """Hourly aggregate of observations (kept after the detailed rows are purged)."""

    __tablename__ = "observation_counters"
    __table_args__ = (check_in("source", OBSERVATION_SOURCES, "source"),)

    content_id: Mapped[int] = mapped_column(
        ForeignKey("content_items.id", ondelete="CASCADE"), primary_key=True
    )
    hour: Mapped[datetime] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(Text, primary_key=True)
    count: Mapped[int] = mapped_column(Integer)


class InstallationSecret(Base):
    """HMAC secret of a UTC day. Deleted after that day: pseudonyms become unlinkable."""

    __tablename__ = "installation_secrets"
    __table_args__ = (CheckConstraint("octet_length(secret) = 32", name="secret"),)

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    secret: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class InstallationDaily(Base):
    """Daily counter and quota of an installation, keyed by its pseudonym of the day."""

    __tablename__ = "installation_daily"
    __table_args__ = (
        PrimaryKeyConstraint("day", "pseudonym"),
        CheckConstraint("octet_length(pseudonym) = 16", name="pseudonym"),
    )

    day: Mapped[date] = mapped_column(Date)
    pseudonym: Mapped[bytes] = mapped_column(LargeBinary)
    items: Mapped[int] = mapped_column(Integer, server_default=sa_text("0"))
    quota: Mapped[int] = mapped_column(Integer)


class ExtensionFilterVersion(Base):
    """Server-driven filters of the extension; a single active version."""

    __tablename__ = "extension_filter_versions"
    __table_args__ = (
        Index(
            "uq_extension_filter_versions_active",
            "active",
            unique=True,
            postgresql_where=sa_text("active"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    version: Mapped[str] = mapped_column(Text, unique=True)
    filters: Mapped[dict[str, Any]] = mapped_column()
    active: Mapped[bool] = mapped_column(server_default=sa_text("false"))
    created_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
