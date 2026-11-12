"""Fact-check V2 tables (lot 5): E2 extraction tracking and micro-batches, scheduler
runs, narratives (groups, journal, window stats, novelty fingerprints) and entity
rarity. See docs/database.md and docs/fact-check.md."""

from __future__ import annotations

import uuid
from datetime import date, datetime
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
    PrimaryKeyConstraint,
    SmallInteger,
    Text,
    func,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from tower.core.db.base import Base
from tower.core.domain.factcheck.values import (
    BatchStatus,
    ExtractionStatus,
    NarrativeEventKind,
    NarrativeState,
    NoveltyKind,
    TitleStatus,
)
from tower.core.models._helpers import check_in

_EMPTY_JSON = sa_text("'{}'::jsonb")
_EMPTY_IDS = sa_text("'{}'::bigint[]")
WINDOWS = ("5m", "1h", "6h")


def _uuid() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), unique=True, server_default=sa_text("gen_random_uuid()")
    )


class ExtractionBatch(Base):
    """One E2 micro-batch (N1): composition, canary, outcome."""

    __tablename__ = "extraction_batches"
    __table_args__ = (
        check_in("status", BatchStatus, "status"),
        Index("ix_extraction_batches_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    content_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger))
    canary_token: Mapped[str | None] = mapped_column(Text)
    """Unique figure of the synthetic canary post (NULL: no canary in this batch)."""
    status: Mapped[str] = mapped_column(Text)
    fidelity_failures: Mapped[int] = mapped_column(SmallInteger, server_default=sa_text("0"))
    contamination: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("false"))
    prompt_version: Mapped[str | None] = mapped_column(Text)
    cost_usd_micros: Mapped[int] = mapped_column(BigInteger, server_default=sa_text("0"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column()


class ContentExtraction(Base):
    """What E2 did with a content (one row per content): extracted, no claim, sampled out."""

    __tablename__ = "content_extractions"
    __table_args__ = (
        check_in("status", ExtractionStatus, "status"),
        Index("ix_content_extractions_created", "created_at"),
    )

    content_id: Mapped[int] = mapped_column(
        ForeignKey("content_items.id", ondelete="CASCADE"), primary_key=True
    )
    level: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("extraction_batches.id", ondelete="SET NULL"), index=True
    )
    position: Mapped[int | None] = mapped_column(SmallInteger)
    """Position in the batch (omission measured by position)."""
    narrative_id: Mapped[int | None] = mapped_column(
        ForeignKey("narratives.id", ondelete="SET NULL")
    )
    """Surging narrative a sampled-out content was counted for."""
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ExtractionAudit(Base):
    """Solo replay of a batched post (omission measurement, 2 %)."""

    __tablename__ = "extraction_audits"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    content_id: Mapped[int] = mapped_column(
        ForeignKey("content_items.id", ondelete="CASCADE"), index=True
    )
    batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("extraction_batches.id", ondelete="SET NULL")
    )
    position: Mapped[int] = mapped_column(SmallInteger)
    batch_size: Mapped[int] = mapped_column(SmallInteger)
    batch_claims: Mapped[int] = mapped_column(SmallInteger)
    solo_claims: Mapped[int] = mapped_column(SmallInteger)
    discordant: Mapped[bool] = mapped_column(Boolean)
    details: Mapped[dict[str, Any]] = mapped_column(server_default=_EMPTY_JSON)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SchedulerRun(Base):
    """One pass of the E4 scheduler (singleton): rate, interval, what moved."""

    __tablename__ = "scheduler_runs"
    __table_args__ = (Index("ix_scheduler_runs_started", "started_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column()
    rate_per_hour: Mapped[float] = mapped_column(Float)
    interval_s: Mapped[int] = mapped_column(Integer)
    stats: Mapped[dict[str, Any]] = mapped_column(server_default=_EMPTY_JSON)
    """Promotions, demotions, enqueued jobs, held back (backpressure), tiers."""


class Narrative(Base):
    """A story told by several claim groups: shares an evidence dossier and an alert,
    never a verdict (docs/factcheck/vagues.md)."""

    __tablename__ = "narratives"
    __table_args__ = (
        check_in("state", NarrativeState, "state"),
        check_in("title_status", TitleStatus, "title_status"),
        CheckConstraint("cardinality(medoid_group_ids) <= 3", name="medoids_max"),
        Index("ix_narratives_state_activity", "state", "last_activity_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[uuid.UUID] = _uuid()
    title: Mapped[str | None] = mapped_column(Text)
    title_status: Mapped[str] = mapped_column(Text, server_default=sa_text("'none'"))
    state: Mapped[str] = mapped_column(Text)
    theme: Mapped[str | None] = mapped_column(Text)
    founder_group_id: Mapped[int] = mapped_column(
        ForeignKey("claim_groups.id", ondelete="RESTRICT")
    )
    medoid_group_ids: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), server_default=_EMPTY_IDS
    )
    """Founder (fixed) then the two groups of highest reach (re-elected at most daily)."""
    medoids_elected_at: Mapped[datetime | None] = mapped_column()
    previous_narrative_id: Mapped[int | None] = mapped_column(
        ForeignKey("narratives.id", ondelete="SET NULL")
    )
    """Resurgence: the closed narrative this one continues (inherits its dossier)."""
    merged_into_id: Mapped[int | None] = mapped_column(
        ForeignKey("narratives.id", ondelete="SET NULL")
    )
    representative_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("claim_groups.id", ondelete="SET NULL")
    )
    """Group investigated once for the whole narrative in wave mode."""
    split_suggested: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("false"))
    state_changed_at: Mapped[datetime | None] = mapped_column()
    surge_started_at: Mapped[datetime | None] = mapped_column()
    below_watch_since: Mapped[datetime | None] = mapped_column()
    opened_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_activity_at: Mapped[datetime] = mapped_column(server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class NarrativeGroup(Base):
    """Group <-> narrative, with history (reversible merges and splits)."""

    __tablename__ = "narrative_groups"
    __table_args__ = (
        PrimaryKeyConstraint("narrative_id", "group_id", "added_at"),
        Index(
            "uq_narrative_groups_active_group",
            "group_id",
            unique=True,
            postgresql_where=sa_text("removed_at IS NULL"),
        ),
    )

    narrative_id: Mapped[int] = mapped_column(ForeignKey("narratives.id", ondelete="CASCADE"))
    group_id: Mapped[int] = mapped_column(ForeignKey("claim_groups.id", ondelete="CASCADE"))
    added_at: Mapped[datetime] = mapped_column(server_default=func.now())
    removed_at: Mapped[datetime | None] = mapped_column()
    signals: Mapped[dict[str, Any]] = mapped_column(server_default=_EMPTY_JSON)
    """Attachment evidence: signals, anchors, medoid similarity."""
    reason: Mapped[str | None] = mapped_column(Text)


class NarrativeEvent(Base):
    __tablename__ = "narrative_events"
    __table_args__ = (
        check_in("kind", NarrativeEventKind, "kind"),
        Index("ix_narrative_events_narrative", "narrative_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    narrative_id: Mapped[int] = mapped_column(ForeignKey("narratives.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text)
    other_narrative_id: Mapped[int | None] = mapped_column(
        ForeignKey("narratives.id", ondelete="SET NULL")
    )
    group_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), server_default=_EMPTY_IDS)
    from_state: Mapped[str | None] = mapped_column(Text)
    to_state: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(server_default=_EMPTY_JSON)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class NarrativeStat(Base):
    """Window aggregate of a narrative (5 min, 1 h, 6 h) written by the scheduler."""

    __tablename__ = "narrative_stats"
    __table_args__ = (
        PrimaryKeyConstraint("narrative_id", "span", "window_end"),
        check_in("span", WINDOWS, "span"),
        Index("ix_narrative_stats_window_end", "window_end"),
    )

    narrative_id: Mapped[int] = mapped_column(ForeignKey("narratives.id", ondelete="CASCADE"))
    span: Mapped[str] = mapped_column(Text)
    """Window length: ``5m``, ``1h`` or ``6h``."""
    window_end: Mapped[datetime] = mapped_column()
    posts: Mapped[int] = mapped_column(Integer)
    flow_posts: Mapped[int] = mapped_column(Integer)
    """All observed posts of the window (denominator of the share)."""
    share: Mapped[float] = mapped_column(Float)
    accounts: Mapped[int] = mapped_column(Integer)
    platforms: Mapped[int] = mapped_column(SmallInteger)
    reach: Mapped[int] = mapped_column(BigInteger, server_default=sa_text("0"))
    score: Mapped[float] = mapped_column(Float)
    """Poisson-type deviation: (observed - expected) / sqrt(expected + 5)."""


class NarrativeNovelty(Base):
    """Fingerprint of a novelty (number, link, mention, media, entity) in a surging
    narrative; forced extraction once it "takes" (3 posts from 3 accounts)."""

    __tablename__ = "narrative_novelty"
    __table_args__ = (
        PrimaryKeyConstraint("narrative_id", "fingerprint"),
        check_in("kind", NoveltyKind, "kind"),
    )

    narrative_id: Mapped[int] = mapped_column(ForeignKey("narratives.id", ondelete="CASCADE"))
    fingerprint: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    posts: Mapped[int] = mapped_column(Integer, server_default=sa_text("0"))
    accounts: Mapped[int] = mapped_column(Integer, server_default=sa_text("0"))
    account_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), server_default=_EMPTY_IDS)
    """Distinct author accounts (capped at 10: enough to test the threshold)."""
    best_content_id: Mapped[int | None] = mapped_column(
        ForeignKey("content_items.id", ondelete="SET NULL")
    )
    best_engagement: Mapped[int] = mapped_column(BigInteger, server_default=sa_text("0"))
    forced_at: Mapped[datetime | None] = mapped_column()
    first_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())


class EntityRarity(Base):
    """Daily count of claims citing an entity (rare entities anchor narratives)."""

    __tablename__ = "entity_rarity"
    __table_args__ = (PrimaryKeyConstraint("entity_key", "day"),)

    entity_key: Mapped[str] = mapped_column(Text)
    """QID, else normalized name."""
    day: Mapped[date] = mapped_column(Date)
    count: Mapped[int] = mapped_column(Integer, server_default=sa_text("0"))
