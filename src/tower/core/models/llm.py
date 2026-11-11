"""Model router tables: models, versioned routes, call log (content cache), free
allocation counters and budget counters. See docs/llm.md."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from tower.core.db.base import Base
from tower.core.domain.llm import CallStatus, Level, Provider
from tower.core.models._helpers import check_in

_LEVELS = [*(lvl.value for lvl in Level), "*"]


class LLMModel(Base):
    __tablename__ = "llm_models"
    __table_args__ = (
        UniqueConstraint("provider", "model"),
        check_in("provider", Provider, "provider"),
        CheckConstraint(
            "free_group IS NULL OR free_group IN ('small', 'large')", name="free_group"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    input_usd_per_mtok: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    cached_input_usd_per_mtok: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    output_usd_per_mtok: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    batch_factor: Mapped[Decimal] = mapped_column(Numeric(4, 3), server_default=text("0.5"))
    """Price multiplier of the Batch API."""
    free_group: Mapped[str | None] = mapped_column(Text)
    """``small`` / ``large`` when eligible to OpenAI's free allocation (verified only)."""
    capabilities: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    prices_checked_on: Mapped[date] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LLMRoute(Base):
    """Versioned route: a change inserts a new version and deactivates the previous one."""

    __tablename__ = "llm_routes"
    __table_args__ = (
        UniqueConstraint("role", "level", "version"),
        check_in("level", _LEVELS, "level"),
        Index(
            "uq_llm_routes_active",
            "role",
            "level",
            unique=True,
            postgresql_where=text("active"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    role: Mapped[str] = mapped_column(Text)
    level: Mapped[str] = mapped_column(Text)
    """``N0``..``N4`` or ``*`` (any level)."""
    model_id: Mapped[int] = mapped_column(ForeignKey("llm_models.id", ondelete="RESTRICT"))
    fallback_model_ids: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), server_default=text("'{}'::bigint[]")
    )
    """Ordered fallbacks."""
    params: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    data_sharing_ok: Mapped[bool] = mapped_column(server_default=text("false"))
    version: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(server_default=text("true"))
    created_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LLMCall(Base):
    """One provider call (or cache hit source). A succeeded row is the content cache."""

    __tablename__ = "llm_calls"
    __table_args__ = (
        check_in("status", CallStatus, "status"),
        Index(
            "uq_llm_calls_cache_key_succeeded",
            "cache_key",
            unique=True,
            postgresql_where=text("status = 'succeeded'"),
        ),
        Index("ix_llm_calls_role_created_at", "role", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    cache_key: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    level: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    prompt_id: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(Text)
    route_id: Mapped[int | None] = mapped_column(ForeignKey("llm_routes.id", ondelete="SET NULL"))
    route_version: Mapped[int] = mapped_column(Integer)
    profile_version: Mapped[int | None] = mapped_column(Integer)
    budget_scope: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cached_input_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cost_usd_micros: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    free_tier: Mapped[bool] = mapped_column(server_default=text("false"))
    latency_ms: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    request_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    output: Mapped[dict[str, Any] | None] = mapped_column()
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LLMFreeUsage(Base):
    __tablename__ = "llm_free_usage"
    __table_args__ = (CheckConstraint("free_group IN ('small', 'large')", name="free_group"),)

    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    free_group: Mapped[str] = mapped_column(Text, primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    """UTC day (the allocation resets at 00:00 UTC)."""
    tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))


class BudgetCounter(Base):
    """Budget of a scope for a period: reserve (atomic conditional UPDATE), then settle."""

    __tablename__ = "budget_counters"
    __table_args__ = (
        UniqueConstraint("scope", "period_start"),
        CheckConstraint(
            "spent_usd_micros >= 0 AND reserved_usd_micros >= 0 AND cap_usd_micros >= 0",
            name="amounts",
        ),
        CheckConstraint("period_end > period_start", name="period"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    scope: Mapped[str] = mapped_column(Text)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    cap_usd_micros: Mapped[int] = mapped_column(BigInteger)
    spent_usd_micros: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    reserved_usd_micros: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    thresholds_notified: Mapped[list[int]] = mapped_column(
        ARRAY(SmallInteger), server_default=text("'{}'::smallint[]")
    )
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class BudgetReservation(Base):
    """Outstanding hold on a budget counter or the free allocation (released on settle,
    or by maintenance once expired: a process may die between reserve and settle)."""

    __tablename__ = "budget_reservations"
    __table_args__ = (
        CheckConstraint("kind IN ('budget', 'free')", name="kind"),
        CheckConstraint("amount > 0", name="amount"),
        CheckConstraint("(kind = 'budget') = (counter_id IS NOT NULL)", name="counter_for_budget"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(Text)
    """Budget scope (``global``, ``provider:x``...) or ``<provider>:<free_group>``."""
    counter_id: Mapped[int | None] = mapped_column(
        ForeignKey("budget_counters.id", ondelete="CASCADE"), index=True
    )
    period_start: Mapped[date] = mapped_column(Date)
    """Counter period, or the UTC day of the free allocation."""
    amount: Mapped[int] = mapped_column(BigInteger)
    """USD micros (budget) or tokens (free)."""
    ref: Mapped[str] = mapped_column(Text)
    """Short identifier of the call (``llm:<role>``, ``radar:poll:<id>``)."""
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(index=True)
