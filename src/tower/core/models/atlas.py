"""Complete Atlas tables (lot 6): LEGI legal mirror, country registries, news index,
reliability registry history, synchronization state and confidence calibration models.
See docs/database.md and docs/factcheck/donnees.md.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from tower.core.db.base import Base
from tower.core.domain.factcheck.atlas import (
    ElectionKind,
    JurisdictionSourceType,
    LegalAccess,
    NewsFeedKind,
    NewsOrigin,
)
from tower.core.models._helpers import check_in
from tower.core.models.factcheck import TIERS

__all__ = [
    "AtlasSyncState",
    "ConfidenceModel",
    "ElectionEntry",
    "JurisdictionSource",
    "LegalArticle",
    "LegalPortal",
    "NewsFeed",
    "NewsItem",
    "SourceRegistryVersion",
]

_EMPTY_JSON = sa_text("'{}'::jsonb")
_COUNTRY_CHECK = "{col} ~ '^([A-Z]{{2}}(-[A-Z0-9]{{1,3}})?|EU|INT)$'"


class LegalArticle(Base):
    """One version of a legal article (LEGI mirror): the judge always gets it with its
    dates in force. Full-text search on disk (French ``tsvector`` + GIN)."""

    __tablename__ = "legal_articles"
    __table_args__ = (
        UniqueConstraint("source", "article_id", name="uq_legal_articles_source_article"),
        CheckConstraint(
            "valid_to IS NULL OR valid_from IS NULL OR valid_from <= valid_to",
            name="validity_order",
        ),
        Index("ix_legal_articles_lookup", "jurisdiction", "text_id", "number", "valid_from"),
        Index("ix_legal_articles_tsv", "tsv", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    jurisdiction: Mapped[str] = mapped_column(Text)
    """``FR`` for LEGI."""
    source: Mapped[str] = mapped_column(Text)
    """``legi``."""
    article_id: Mapped[str] = mapped_column(Text)
    """``LEGIARTI…``: one id per version of the article."""
    text_id: Mapped[str] = mapped_column(Text)
    """``LEGITEXT…`` (or ``JORFTEXT…``) of the code or text."""
    text_title: Mapped[str] = mapped_column(Text)
    number: Mapped[str | None] = mapped_column(Text)
    """``432-13``, ``L. 1234-5``…"""
    status: Mapped[str] = mapped_column(Text)
    """LEGI ``ETAT`` as published (``VIGUEUR``, ``ABROGE``, ``MODIFIE``…)."""
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    """``NULL`` = still in force (LEGI ``2999-01-01``)."""
    section_path: Mapped[str | None] = mapped_column(Text)
    """Titles of the enclosing sections (``Livre IV > Titre III > …``)."""
    body: Mapped[str] = mapped_column(Text)
    """Plain text of the article (open licence: stored in full)."""
    url: Mapped[str] = mapped_column(Text)
    sha256: Mapped[bytes] = mapped_column(LargeBinary)
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('french'::regconfig, coalesce(number, '') || ' ' || body)",
            persisted=True,
        ),
    )
    synced_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LegalPortal(Base):
    """Official legal portal of a country (ELI support, access mode)."""

    __tablename__ = "legal_portals"
    __table_args__ = (
        check_in("access", LegalAccess, "access"),
        CheckConstraint(_COUNTRY_CHECK.format(col="country"), name="country_format"),
        UniqueConstraint("country", "url", name="uq_legal_portals_country_url"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    country: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    domain: Mapped[str] = mapped_column(Text)
    eli: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("false"))
    access: Mapped[str] = mapped_column(Text)
    adapter: Mapped[str | None] = mapped_column(Text)
    """Atlas adapter serving it (``legi_mirror``, ``eurlex``, ``fedlex``…), if any."""
    notes: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("true"))
    version: Mapped[int] = mapped_column(Integer, server_default=sa_text("1"))
    updated_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class JurisdictionSource(Base):
    """Country × source type → Atlas adapter or official domain, with its tier."""

    __tablename__ = "jurisdiction_sources"
    __table_args__ = (
        check_in("source_type", JurisdictionSourceType, "source_type"),
        check_in("tier", TIERS, "tier"),
        CheckConstraint(_COUNTRY_CHECK.format(col="country"), name="country_format"),
        CheckConstraint("adapter IS NOT NULL OR domain IS NOT NULL", name="adapter_or_domain"),
        UniqueConstraint("country", "source_type", "key", name="uq_jurisdiction_sources_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    country: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(Text)
    key: Mapped[str] = mapped_column(Text)
    """``adapter`` when set, else ``domain`` (idempotent seeding)."""
    adapter: Mapped[str | None] = mapped_column(Text)
    domain: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    tier: Mapped[str] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("true"))
    version: Mapped[int] = mapped_column(Integer, server_default=sa_text("1"))
    updated_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ElectionEntry(Base):
    """A scheduled election: the thematic risk "elections" rises around it."""

    __tablename__ = "election_calendar"
    __table_args__ = (
        check_in("kind", ElectionKind, "kind"),
        CheckConstraint(_COUNTRY_CHECK.format(col="country"), name="country_format"),
        CheckConstraint("second_round IS NULL OR second_round >= first_round", name="rounds_order"),
        UniqueConstraint("country", "kind", "first_round", name="uq_election_calendar_entry"),
        Index("ix_election_calendar_first_round", "first_round"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    country: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    first_round: Mapped[date] = mapped_column(Date)
    second_round: Mapped[date | None] = mapped_column(Date)
    source_url: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, server_default=sa_text("1"))
    updated_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class NewsFeed(Base):
    """Reference news feed (RSS/Atom) of a country and language, polled every 15 min."""

    __tablename__ = "news_feeds"
    __table_args__ = (
        check_in("kind", NewsFeedKind, "kind"),
        CheckConstraint(_COUNTRY_CHECK.format(col="country"), name="country_format"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    country: Mapped[str] = mapped_column(Text)
    lang: Mapped[str] = mapped_column(Text)
    publisher: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text, unique=True)
    domain: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("true"))
    etag: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[str | None] = mapped_column(Text)
    last_polled_at: Mapped[datetime | None] = mapped_column()
    last_status: Mapped[int | None] = mapped_column(SmallInteger)
    failures: Mapped[int] = mapped_column(Integer, server_default=sa_text("0"))
    """Consecutive failures (reset on success)."""
    version: Mapped[int] = mapped_column(Integer, server_default=sa_text("1"))
    updated_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class NewsItem(Base):
    """Recent headline (``news_index``): title, lead, date, URL, publisher only — never the
    full article. Kept 90 days. ``cluster_id`` groups the republications of one wire
    story (SimHash of title + lead): they count as one source."""

    __tablename__ = "news_index"
    __table_args__ = (
        check_in("origin", NewsOrigin, "origin"),
        Index("ix_news_index_published_at", "published_at"),
        Index("ix_news_index_tsv", "tsv", postgresql_using="gin"),
        Index("ix_news_index_b0", "simhash_b0", "published_at"),
        Index("ix_news_index_b1", "simhash_b1", "published_at"),
        Index("ix_news_index_b2", "simhash_b2", "published_at"),
        Index("ix_news_index_b3", "simhash_b3", "published_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    feed_id: Mapped[int | None] = mapped_column(
        ForeignKey("news_feeds.id", ondelete="SET NULL"), index=True
    )
    origin: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    canonical_url: Mapped[str] = mapped_column(Text, unique=True)
    publisher: Mapped[str] = mapped_column(Text)
    domain: Mapped[str] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(Text)
    lang: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    """Lead (chapeau), HTML stripped, at most 600 characters."""
    published_at: Mapped[datetime] = mapped_column()
    fetched_at: Mapped[datetime] = mapped_column(server_default=func.now())
    simhash: Mapped[int] = mapped_column(BigInteger)
    simhash_b0: Mapped[int] = mapped_column(Integer)
    simhash_b1: Mapped[int] = mapped_column(Integer)
    simhash_b2: Mapped[int] = mapped_column(Integer)
    simhash_b3: Mapped[int] = mapped_column(Integer)
    cluster_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    """Id of the first item of its near-duplicate cluster (itself when first)."""
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('simple'::regconfig, title || ' ' || coalesce(summary, ''))",
            persisted=True,
        ),
    )


class SourceRegistryVersion(Base):
    """History of ``source_registry``: one snapshot per version (published registry)."""

    __tablename__ = "source_registry_versions"
    __table_args__ = (
        UniqueConstraint("registry_id", "version", name="uq_source_registry_versions"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    registry_id: Mapped[int] = mapped_column(
        ForeignKey("source_registry.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict[str, Any]] = mapped_column()
    change: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    changed_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AtlasSyncState(Base):
    """Progress of the Atlas synchronizations (``legi`` = last applied archive)."""

    __tablename__ = "atlas_sync_state"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    state: Mapped[dict[str, Any]] = mapped_column(server_default=_EMPTY_JSON)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ConfidenceModel(Base):
    """Recalibrated confidence model (pipeline.md E10): produced only above the data
    thresholds, activated only when its held-out measurement beats the current one."""

    __tablename__ = "confidence_models"
    __table_args__ = (
        check_in("kind", ("reduced", "full"), "kind"),
        Index(
            "uq_confidence_models_active", "active", unique=True, postgresql_where=sa_text("active")
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    """``reduced`` (intercept + 3-4 shrunk coefficients) or ``full``."""
    coefficients: Mapped[dict[str, Any]] = mapped_column()
    metrics: Mapped[dict[str, Any]] = mapped_column(server_default=_EMPTY_JSON)
    """Held-out calibration error (ECE) of the candidate and of the current model."""
    reviews: Mapped[int] = mapped_column(Integer)
    refusals: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("false"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    activated_at: Mapped[datetime | None] = mapped_column()
    activated_by: Mapped[str | None] = mapped_column(Text)
