"""Fact-check V1 tables (lot 4): post embeddings (E1), group journal, reference facts,
value bands, processing profiles, level overrides and E1 router models.
See docs/database.md and docs/fact-check.md.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    func,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import ARRAY, DOUBLE_PRECISION, UUID
from sqlalchemy.orm import Mapped, mapped_column

from tower.core.db.base import Base
from tower.core.db.vector import HalfVec
from tower.core.domain.factcheck.values import (
    Band,
    BandSide,
    BandStatus,
    ContextNeed,
    FactOrigin,
    FactStatus,
    GroupEventKind,
    OverrideTarget,
    Veracity,
)
from tower.core.models._helpers import check_in
from tower.core.models.factcheck import LEVELS

_TEXT_ARRAY = ARRAY(Text)
_EMPTY_ARRAY = sa_text("'{}'::text[]")
_EMPTY_JSON = sa_text("'{}'::jsonb")
E1_LEVELS = ("N0", "N1")


def _uuid() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), unique=True, server_default=sa_text("gen_random_uuid()")
    )


class ContentEmbedding(Base):
    """E1 vector and routing decision of a content. Partitioned by week on
    ``created_at``, **no vector index**, partitions dropped after 14 days."""

    __tablename__ = "content_embeddings"
    __table_args__ = (
        PrimaryKeyConstraint("content_id", "created_at"),
        check_in("level", E1_LEVELS, "level"),
        CheckConstraint("score IS NULL OR score BETWEEN 0 AND 1", name="score_range"),
        {"postgresql_partition_by": "RANGE (created_at)"},
    )

    content_id: Mapped[int] = mapped_column(
        ForeignKey("content_items.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    embedding: Mapped[list[float]] = mapped_column(HalfVec())
    model: Mapped[str] = mapped_column(Text)
    lang: Mapped[str] = mapped_column(Text)
    score: Mapped[float | None] = mapped_column(Float)
    """Probability "verifiable" (NULL under the permissive rule: no active model)."""
    level: Mapped[str] = mapped_column(Text)
    """Initial level: N1 (verifiable or sampled) or N0 (discarded)."""
    sampled: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("false"))
    """Discarded content kept in the random sample (false negatives)."""
    router_version: Mapped[int | None] = mapped_column(Integer)


class ClaimGroupEvent(Base):
    """Group journal: creation, joins, merges, splits, level changes (with author)."""

    __tablename__ = "claim_group_events"
    __table_args__ = (
        check_in("kind", GroupEventKind, "kind"),
        Index("ix_claim_group_events_group", "group_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("claim_groups.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text)
    other_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("claim_groups.id", ondelete="SET NULL")
    )
    """Merge source or target, split result, suggested group (``related``)."""
    claim_ids: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), server_default=sa_text("'{}'::bigint[]")
    )
    from_level: Mapped[str | None] = mapped_column(Text)
    to_level: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(server_default=_EMPTY_JSON)
    """E3 details (rule, similarity, arbiter reason), moved claims for an undo."""
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ReferenceFact(Base):
    """Reference value of an indicator (Q9): serves only once validated by a reviewer."""

    __tablename__ = "reference_facts"
    __table_args__ = (
        check_in("status", FactStatus, "status"),
        check_in("origin", FactOrigin, "origin"),
        CheckConstraint("period_start <= period_end", name="period_order"),
        Index("ix_reference_facts_lookup", "indicator", "territory", "period_end"),
        Index("ix_reference_facts_subject_qid", "subject_qid"),
        Index("ix_reference_facts_aliases", "aliases", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[uuid.UUID] = _uuid()
    indicator: Mapped[str] = mapped_column(Text)
    """Stable key (``fr.unemployment_rate``)."""
    label: Mapped[str] = mapped_column(Text)
    """Human label (French)."""
    definition_variant: Mapped[str | None] = mapped_column(Text)
    """``ilo`` (BIT), ``cat_a``…; NULL = the default definition of the indicator."""
    subject_qid: Mapped[str | None] = mapped_column(Text)
    aliases: Mapped[list[str]] = mapped_column(_TEXT_ARRAY, server_default=_EMPTY_ARRAY)
    """Normalized subjects that resolve to this indicator (deterministic matching)."""
    value: Mapped[Decimal] = mapped_column(Numeric)
    unit: Mapped[str] = mapped_column(Text)
    unit_scale: Mapped[str | None] = mapped_column(Text)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    territory: Mapped[str] = mapped_column(Text)
    """ISO 3166-1/2, ``EU`` or ``INT``."""
    source_url: Mapped[str] = mapped_column(Text)
    source_name: Mapped[str] = mapped_column(Text)
    published_at: Mapped[date] = mapped_column(Date)
    next_publication: Mapped[date | None] = mapped_column(Date)
    previous_fact_id: Mapped[int | None] = mapped_column(
        ForeignKey("reference_facts.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(Text)
    origin_verdict_id: Mapped[int | None] = mapped_column(
        ForeignKey("verdicts.id", ondelete="SET NULL")
    )
    external_key: Mapped[str | None] = mapped_column(Text, unique=True)
    """Idempotence key of an automatic feed (``insee:<idbank>:<period>``)."""
    created_by: Mapped[str] = mapped_column(Text)
    validated_by: Mapped[str | None] = mapped_column(Text)
    validated_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class VerdictBand(Base):
    """Reference fact x band x side -> outcome validated once by a reviewer (Q9)."""

    __tablename__ = "verdict_bands"
    __table_args__ = (
        check_in("band", Band, "band"),
        check_in("side", BandSide, "side"),
        check_in("status", BandStatus, "status"),
        check_in("veracity", Veracity, "veracity"),
        check_in("context_need", ContextNeed, "context_need"),
        Index(
            "uq_verdict_bands_active",
            "reference_fact_id",
            "band",
            "side",
            unique=True,
            postgresql_where=sa_text("status = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[uuid.UUID] = _uuid()
    reference_fact_id: Mapped[int] = mapped_column(
        ForeignKey("reference_facts.id", ondelete="CASCADE")
    )
    band: Mapped[str] = mapped_column(Text)
    side: Mapped[str] = mapped_column(Text)
    veracity: Mapped[str] = mapped_column(Text)
    context_need: Mapped[str] = mapped_column(Text)
    context_types: Mapped[list[str]] = mapped_column(_TEXT_ARRAY, server_default=_EMPTY_ARRAY)
    source_verdict_id: Mapped[int | None] = mapped_column(
        ForeignKey("verdicts.id", ondelete="SET NULL")
    )
    """Validated verdict whose note is shown to readers of inheriting variants."""
    status: Mapped[str] = mapped_column(Text)
    validated_by: Mapped[str] = mapped_column(Text)
    validated_at: Mapped[datetime] = mapped_column(server_default=func.now())
    revoked_by: Mapped[str | None] = mapped_column(Text)
    revoked_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ProcessingProfile(Base):
    """Versioned per-level settings (niveaux.md); one active version, safety bounds
    checked by the use case. No active row = the default profile from the settings."""

    __tablename__ = "processing_profiles"
    __table_args__ = (
        Index(
            "uq_processing_profiles_active",
            "active",
            unique=True,
            postgresql_where=sa_text("active"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, unique=True)
    settings: Mapped[dict[str, Any]] = mapped_column()
    active: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("false"))
    based_on_version: Mapped[int | None] = mapped_column(Integer)
    author: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    activated_at: Mapped[datetime | None] = mapped_column()
    activated_by: Mapped[str | None] = mapped_column(Text)


class LevelOverride(Base):
    """Manual level forcing of a content, group or account, with expiry and reason."""

    __tablename__ = "level_overrides"
    __table_args__ = (
        check_in("target_type", OverrideTarget, "target_type"),
        check_in("level", LEVELS, "level"),
        CheckConstraint("char_length(reason) > 0", name="reason_required"),
        Index("ix_level_overrides_target", "target_type", "target_id", "expires_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[uuid.UUID] = _uuid()
    target_type: Mapped[str] = mapped_column(Text)
    target_id: Mapped[int] = mapped_column(BigInteger)
    """Internal id of ``content_items``, ``claim_groups`` or ``platform_accounts``."""
    level: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column()
    author: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    revoked_at: Mapped[datetime | None] = mapped_column()
    revoked_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RouterModel(Base):
    """Weights of the E1 logistic regression (pure Python at inference)."""

    __tablename__ = "router_models"
    __table_args__ = (
        Index("uq_router_models_active", "active", unique=True, postgresql_where=sa_text("active")),
        CheckConstraint("cardinality(weights) = dims", name="weights_dims"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, unique=True)
    embedding_model: Mapped[str] = mapped_column(Text)
    dims: Mapped[int] = mapped_column(Integer)
    weights: Mapped[list[float]] = mapped_column(ARRAY(DOUBLE_PRECISION))
    bias: Mapped[float] = mapped_column(DOUBLE_PRECISION)
    metrics: Mapped[dict[str, Any]] = mapped_column(server_default=_EMPTY_JSON)
    """Held-out recall, precision, AUC, sample sizes by stratum."""
    sample_size: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("false"))
    trained_at: Mapped[datetime] = mapped_column(server_default=func.now())
    activated_at: Mapped[datetime | None] = mapped_column()
    activated_by: Mapped[str | None] = mapped_column(Text)
