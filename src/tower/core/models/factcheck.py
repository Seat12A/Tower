"""Fact-check tables of lot 3 (claims, groups, investigations, evidence, verdicts,
notes, reviews, source registry and cache). See docs/database.md and docs/fact-check.md.

Lot 3 creates one claim group per claim; lot 4 adds grouping without rewriting.
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
    LargeBinary,
    Numeric,
    PrimaryKeyConstraint,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from tower.core.db.base import Base
from tower.core.db.vector import HalfVec
from tower.core.domain.factcheck.values import (
    Agreement,
    Band,
    BandSide,
    ClaimGroupState,
    ClaimType,
    ContextNeed,
    ContextType,
    Depth,
    InvestigationStage,
    NoteStatus,
    Polarity,
    Regime,
    ReuseRule,
    ReviewDecision,
    ReviewOutcome,
    TranslationOrigin,
    Veracity,
    VerdictStatus,
)
from tower.core.models._helpers import check_in

LEVELS = ("N0", "N1", "N2", "N3", "N4")
SOURCE_MODALITIES = ("text", "image", "video")
TIERS = ("primary", "secondary", "tertiary", "avoid")
REGISTRY_KINDS = (
    "institution",
    "statistics",
    "court",
    "parliament",
    "agency",
    "press",
    "encyclopedia",
    "fact_checker",
    "academic",
    "platform",
    "other",
)
_TEXT_ARRAY = ARRAY(Text)
_EMPTY_ARRAY = sa_text("'{}'::text[]")
_EMPTY_JSON = sa_text("'{}'::jsonb")
_EMPTY_LIST = sa_text("'[]'::jsonb")


def _uuid() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), unique=True, server_default=sa_text("gen_random_uuid()")
    )


class Claim(Base):
    """Canonical claim extracted by E2, with its normalized signature (typed columns)."""

    __tablename__ = "claims"
    __table_args__ = (
        check_in("claim_type", ClaimType, "claim_type"),
        check_in("polarity", Polarity, "polarity"),
        check_in("source_modality", SOURCE_MODALITIES, "source_modality"),
        CheckConstraint("char_length(text_original) <= 300", name="text_original_len"),
        CheckConstraint("char_length(text_fr) <= 300", name="text_fr_len"),
        CheckConstraint("verifiability BETWEEN 0 AND 1", name="verifiability_range"),
        Index("ix_claims_primary_jurisdiction", "primary_jurisdiction"),
        Index("ix_claims_jurisdictions", "jurisdictions", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[uuid.UUID] = _uuid()
    lang_original: Mapped[str] = mapped_column(Text)
    text_original: Mapped[str] = mapped_column(Text)
    text_fr: Mapped[str] = mapped_column(Text)
    """French pivot (equal to ``text_original`` for French claims)."""
    claim_type: Mapped[str] = mapped_column(Text)
    # Signature (language-neutral fields, extracted from the original text).
    subject: Mapped[str | None] = mapped_column(Text)
    subject_qid: Mapped[str | None] = mapped_column(Text)
    predicate: Mapped[str | None] = mapped_column(Text)
    value_low: Mapped[Decimal | None] = mapped_column(Numeric)
    value_high: Mapped[Decimal | None] = mapped_column(Numeric)
    unit: Mapped[str | None] = mapped_column(Text)
    unit_scale: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str | None] = mapped_column(Text)
    value_date: Mapped[date | None] = mapped_column(Date)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    place: Mapped[str | None] = mapped_column(Text)
    polarity: Mapped[str] = mapped_column(Text, server_default=sa_text("'affirmative'"))
    invoked_source: Mapped[str | None] = mapped_column(Text)
    definition_variant: Mapped[str | None] = mapped_column(Text)
    comparison_base: Mapped[str | None] = mapped_column(Text)
    jurisdictions: Mapped[list[str]] = mapped_column(_TEXT_ARRAY, server_default=_EMPTY_ARRAY)
    primary_jurisdiction: Mapped[str | None] = mapped_column(Text)
    entities: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=_EMPTY_LIST)
    """``[{"name", "role", "qid"}]`` (role: ``EntityRole``)."""
    theme: Mapped[str | None] = mapped_column(Text)
    verifiability: Mapped[float] = mapped_column(Float)
    context_hints: Mapped[list[str]] = mapped_column(_TEXT_ARRAY, server_default=_EMPTY_ARRAY)
    source_modality: Mapped[str] = mapped_column(Text, server_default=sa_text("'text'"))
    thread_context: Mapped[str | None] = mapped_column(Text)
    """Neutral rephrasing of the parent or quoted post (never the raw text)."""
    prompt_version: Mapped[str] = mapped_column(Text)
    # Lot 4 (E3): vectors are never indexed on claims (exact comparisons only).
    embedding: Mapped[list[float] | None] = mapped_column(HalfVec())
    """Vector of ``text_original``."""
    embedding_pivot: Mapped[list[float] | None] = mapped_column(HalfVec())
    """Vector of ``text_fr`` for non-French claims (NULL for French: same as embedding)."""
    embedding_model: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ContentClaim(Base):
    __tablename__ = "content_claims"
    __table_args__ = (PrimaryKeyConstraint("content_id", "claim_id"),)

    content_id: Mapped[int] = mapped_column(ForeignKey("content_items.id", ondelete="CASCADE"))
    claim_id: Mapped[int] = mapped_column(ForeignKey("claims.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(SmallInteger, server_default=sa_text("0"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ClaimGroup(Base):
    """Group of claims that say the same thing, verifiable by the same evidence (E3).

    Joining a group requires an identical normalized signature (docs/fact-check.md):
    every member is covered by the group verdict."""

    __tablename__ = "claim_groups"
    __table_args__ = (
        check_in("level", LEVELS, "level"),
        check_in("state", ClaimGroupState, "state"),
        check_in("band", Band, "band"),
        check_in("band_side", BandSide, "band_side"),
        Index(
            "ix_claim_groups_representative_embedding",
            "representative_embedding",
            postgresql_using="hnsw",
            postgresql_ops={"representative_embedding": "halfvec_cosine_ops"},
        ),
        Index("ix_claim_groups_reference_fact", "reference_fact_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[uuid.UUID] = _uuid()
    representative_claim_id: Mapped[int] = mapped_column(
        ForeignKey("claims.id", ondelete="RESTRICT"), index=True
    )
    level: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    reach: Mapped[int] = mapped_column(BigInteger, server_default=sa_text("0"))
    active_verdict_id: Mapped[int | None] = mapped_column(
        ForeignKey("verdicts.id", ondelete="SET NULL", use_alter=True)
    )
    # Lot 4
    representative_embedding: Mapped[list[float] | None] = mapped_column(HalfVec())
    """Pivot-space vector of the representative claim: the only vector index (HNSW)."""
    reference_fact_id: Mapped[int | None] = mapped_column(
        ForeignKey("reference_facts.id", ondelete="SET NULL")
    )
    band: Mapped[str | None] = mapped_column(Text)
    band_side: Mapped[str | None] = mapped_column(Text)
    merged_into_id: Mapped[int | None] = mapped_column(
        ForeignKey("claim_groups.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ClaimGroupMember(Base):
    """Claim ↔ group, with history (``removed_at``) for reversible merges and splits."""

    __tablename__ = "claim_group_members"
    __table_args__ = (
        PrimaryKeyConstraint("group_id", "claim_id"),
        Index(
            "uq_claim_group_members_active_claim",
            "claim_id",
            unique=True,
            postgresql_where=sa_text("removed_at IS NULL"),
        ),
    )

    group_id: Mapped[int] = mapped_column(ForeignKey("claim_groups.id", ondelete="CASCADE"))
    claim_id: Mapped[int] = mapped_column(ForeignKey("claims.id", ondelete="CASCADE"))
    added_at: Mapped[datetime] = mapped_column(server_default=func.now())
    removed_at: Mapped[datetime | None] = mapped_column()
    reason: Mapped[str | None] = mapped_column(Text)


class Investigation(Base):
    """One investigation of a group (E6 to E9). Each stage is a job; intermediate model
    outputs are kept here so a crashed job never pays a finished stage again."""

    __tablename__ = "investigations"
    __table_args__ = (
        check_in("level", LEVELS, "level"),
        check_in("depth", Depth, "depth"),
        check_in("stage", InvestigationStage, "stage"),
        check_in("agreement", Agreement, "agreement"),
        Index("ix_investigations_group", "claim_group_id"),
        Index("ix_investigations_stage", "stage", "updated_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[uuid.UUID] = _uuid()
    claim_group_id: Mapped[int] = mapped_column(ForeignKey("claim_groups.id", ondelete="CASCADE"))
    content_id: Mapped[int | None] = mapped_column(
        ForeignKey("content_items.id", ondelete="SET NULL")
    )
    """Representative content (the post that triggered the investigation)."""
    level: Mapped[str] = mapped_column(Text)
    depth: Mapped[str] = mapped_column(Text)
    stage: Mapped[str] = mapped_column(Text)
    round: Mapped[int] = mapped_column(SmallInteger, server_default=sa_text("1"))
    """1, then 2 after an escalation (extra collection and second judge pass)."""
    cost_usd_micros: Mapped[int] = mapped_column(BigInteger, server_default=sa_text("0"))
    framing: Mapped[dict[str, Any] | None] = mapped_column()
    queries: Mapped[dict[str, Any] | None] = mapped_column()
    judgement: Mapped[dict[str, Any] | None] = mapped_column()
    second_opinion: Mapped[dict[str, Any] | None] = mapped_column()
    objection: Mapped[dict[str, Any] | None] = mapped_column()
    second_pass: Mapped[dict[str, Any] | None] = mapped_column()
    agreement: Mapped[str | None] = mapped_column(Text)
    flags: Mapped[dict[str, Any]] = mapped_column(server_default=_EMPTY_JSON)
    """``conflicting_sources``, ``refused``, ``budget_incomplete``, ``cap_reached``…"""
    error: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column()


class Source(Base):
    """A retrieved source. Its link is always kept; only cited passages survive."""

    __tablename__ = "sources"
    __table_args__ = (
        check_in("tier", TIERS, "tier"),
        Index("ix_sources_domain", "domain"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    canonical_url: Mapped[str] = mapped_column(Text, unique=True)
    original_url: Mapped[str] = mapped_column(Text)
    adapter: Mapped[str] = mapped_column(Text)
    """``wikipedia``, ``wikidata``, ``factcheck_tools``, ``brave``, ``legifrance``…"""
    stable_id: Mapped[str | None] = mapped_column(Text)
    """Wikipedia revision, QID, LEGI article id, INSEE series…"""
    publisher: Mapped[str | None] = mapped_column(Text)
    domain: Mapped[str] = mapped_column(Text)
    tier: Mapped[str] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    lang: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column()
    retrieved_at: Mapped[datetime] = mapped_column(server_default=func.now())
    sha256: Mapped[bytes | None] = mapped_column(LargeBinary)
    license: Mapped[str | None] = mapped_column(Text)


class EvidencePassage(Base):
    __tablename__ = "evidence_passages"
    __table_args__ = (UniqueConstraint("investigation_id", "label"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    investigation_id: Mapped[int] = mapped_column(
        ForeignKey("investigations.id", ondelete="CASCADE")
    )
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="RESTRICT"), index=True
    )
    label: Mapped[str] = mapped_column(Text)
    """``P1``…``Pn`` within the investigation."""
    position: Mapped[int] = mapped_column(Integer)
    """Chunk index within the source."""
    text: Mapped[str | None] = mapped_column(Text)
    """Original text; set to NULL at the end of the investigation unless cited."""
    lang: Mapped[str] = mapped_column(Text)
    translation_fr: Mapped[str | None] = mapped_column(Text)
    origin: Mapped[str | None] = mapped_column(Text)
    """Entity the information is attributed to (from the judge)."""
    simhash: Mapped[int] = mapped_column(BigInteger)
    score: Mapped[float] = mapped_column(Float, server_default=sa_text("0"))
    round: Mapped[int] = mapped_column(SmallInteger, server_default=sa_text("1"))
    kept: Mapped[bool] = mapped_column(Boolean, server_default=sa_text("false"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Verdict(Base):
    __tablename__ = "verdicts"
    __table_args__ = (
        check_in("veracity", Veracity, "veracity"),
        check_in("context_need", ContextNeed, "context_need"),
        check_in("status", VerdictStatus, "status"),
        check_in("reuse_rule", ReuseRule, "reuse_rule"),
        CheckConstraint("confidence IS NULL OR confidence BETWEEN 0 AND 1", name="confidence"),
        Index(
            "uq_verdicts_active_group",
            "claim_group_id",
            unique=True,
            postgresql_where=sa_text("status = 'active'"),
        ),
        Index("ix_verdicts_status_drafted", "status", "drafted_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[uuid.UUID] = _uuid()
    claim_group_id: Mapped[int] = mapped_column(ForeignKey("claim_groups.id", ondelete="CASCADE"))
    investigation_id: Mapped[int | None] = mapped_column(
        ForeignKey("investigations.id", ondelete="SET NULL"), unique=True
    )
    veracity: Mapped[str] = mapped_column(Text)
    context_need: Mapped[str] = mapped_column(Text)
    context_types: Mapped[list[str]] = mapped_column(_TEXT_ARRAY, server_default=_EMPTY_ARRAY)
    confidence: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(Text)
    rationale: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=_EMPTY_LIST)
    """Assertions of the justification, each with its citations (copy of ``citations``)."""
    not_in_sources: Mapped[list[str]] = mapped_column(_TEXT_ARRAY, server_default=_EMPTY_ARRAY)
    review_points: Mapped[list[str]] = mapped_column(_TEXT_ARRAY, server_default=_EMPTY_ARRAY)
    flags: Mapped[dict[str, Any]] = mapped_column(server_default=_EMPTY_JSON)
    prompt_versions: Mapped[dict[str, Any]] = mapped_column(server_default=_EMPTY_JSON)
    route_versions: Mapped[dict[str, Any]] = mapped_column(server_default=_EMPTY_JSON)
    expires_at: Mapped[datetime | None] = mapped_column()
    drafted_at: Mapped[datetime | None] = mapped_column()
    """Set when the draft entered the review queue (dossier opened, quota counted)."""
    notified_at: Mapped[datetime | None] = mapped_column()
    """Set by the bot once the draft is displayed in Discord (reconciliation)."""
    validated_at: Mapped[datetime | None] = mapped_column()
    validated_by: Mapped[str | None] = mapped_column(Text)
    # Lot 4: a verdict inherited through a validated value band (never a copy of text).
    reused_from_verdict_id: Mapped[int | None] = mapped_column(
        ForeignKey("verdicts.id", ondelete="SET NULL")
    )
    reuse_rule: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(server_default=sa_text("1"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012


class Citation(Base):
    __tablename__ = "citations"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    verdict_id: Mapped[int] = mapped_column(
        ForeignKey("verdicts.id", ondelete="CASCADE"), index=True
    )
    passage_id: Mapped[int] = mapped_column(
        ForeignKey("evidence_passages.id", ondelete="RESTRICT"), index=True
    )
    quote: Mapped[str] = mapped_column(Text)
    assertion: Mapped[str] = mapped_column(Text)
    valid: Mapped[bool] = mapped_column(Boolean)
    """Quote found verbatim in the original passage text."""
    position: Mapped[int] = mapped_column(SmallInteger, server_default=sa_text("0"))


class ContextNote(Base):
    __tablename__ = "context_notes"
    __table_args__ = (
        check_in("context_type", ContextType, "context_type"),
        check_in("regime", Regime, "regime"),
        check_in("status", NoteStatus, "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[uuid.UUID] = _uuid()
    verdict_id: Mapped[int] = mapped_column(
        ForeignKey("verdicts.id", ondelete="CASCADE"), index=True
    )
    claim_group_id: Mapped[int] = mapped_column(
        ForeignKey("claim_groups.id", ondelete="CASCADE"), index=True
    )
    context_type: Mapped[str | None] = mapped_column(Text)
    regime: Mapped[str] = mapped_column(Text)
    object_type: Mapped[str | None] = mapped_column(Text)
    object_id: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    """Writer's version in the post language (immutable draft)."""
    lang: Mapped[str] = mapped_column(Text)
    links: Mapped[list[str]] = mapped_column(_TEXT_ARRAY, server_default=_EMPTY_ARRAY)
    status: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column()
    checks: Mapped[dict[str, Any]] = mapped_column(server_default=_EMPTY_JSON)
    critic: Mapped[dict[str, Any] | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ContextNoteTranslation(Base):
    """Every language version of a note (the post language included)."""

    __tablename__ = "context_note_translations"
    __table_args__ = (
        UniqueConstraint("note_id", "lang"),
        check_in("origin", TranslationOrigin, "origin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    note_id: Mapped[int] = mapped_column(ForeignKey("context_notes.id", ondelete="CASCADE"))
    lang: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    fact_sheet: Mapped[str | None] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(Text)
    validated_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Review(Base):
    """A reviewer's decision on a draft: learning data (E11)."""

    __tablename__ = "reviews"
    __table_args__ = (
        check_in("decision", ReviewDecision, "decision"),
        check_in("outcome", ReviewOutcome, "outcome"),
        check_in("final_veracity", Veracity, "final_veracity"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    verdict_id: Mapped[int] = mapped_column(
        ForeignKey("verdicts.id", ondelete="CASCADE"), index=True
    )
    dossier_id: Mapped[int | None] = mapped_column(
        ForeignKey("dossiers.id", ondelete="SET NULL"), index=True
    )
    reviewer: Mapped[str] = mapped_column(Text)
    """Actor label (``staff:<discord id>``)."""
    decision: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(Text)
    final_veracity: Mapped[str | None] = mapped_column(Text)
    diff: Mapped[dict[str, Any]] = mapped_column(server_default=_EMPTY_JSON)
    labels: Mapped[list[str]] = mapped_column(_TEXT_ARRAY, server_default=_EMPTY_ARRAY)
    reason: Mapped[str | None] = mapped_column(Text)
    review_seconds: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class DossierClaimGroup(Base):
    __tablename__ = "dossier_claim_groups"
    __table_args__ = (PrimaryKeyConstraint("dossier_id", "claim_group_id"),)

    dossier_id: Mapped[int] = mapped_column(ForeignKey("dossiers.id", ondelete="CASCADE"))
    claim_group_id: Mapped[int] = mapped_column(
        ForeignKey("claim_groups.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SourceRegistryEntry(Base):
    """Minimal reliability registry (domain → tier, kind, country). Everything absent is
    tertiary. ``controlled_by`` and ``statutory_independence`` come with lot 6."""

    __tablename__ = "source_registry"
    __table_args__ = (
        check_in("tier", TIERS, "tier"),
        check_in("kind", REGISTRY_KINDS, "kind"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    domain: Mapped[str] = mapped_column(Text, unique=True)
    """Registrable domain or host suffix (``insee.fr``, ``europa.eu``)."""
    publisher: Mapped[str] = mapped_column(Text)
    tier: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(Text)
    """ISO 3166-1 alpha-2, ``EU`` or ``INT``."""
    reason: Mapped[str | None] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(Text, server_default=sa_text("'seed'"))
    version: Mapped[int] = mapped_column(server_default=sa_text("1"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SourceCacheEntry(Base):
    """Response cache of knowledge sources and search: a request is never paid twice
    while fresh. ``key`` = SHA-256 of (adapter, canonical request)."""

    __tablename__ = "source_cache"

    key: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    adapter: Mapped[str] = mapped_column(Text)
    response: Mapped[dict[str, Any]] = mapped_column()
    fetched_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(index=True)
