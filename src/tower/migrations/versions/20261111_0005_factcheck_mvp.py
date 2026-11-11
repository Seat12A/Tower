"""Fact-check MVP (lot 3): claims, groups, investigations, evidence, verdicts, notes,
reviews, minimal source registry and source cache; ``dossiers.content_id``.

Expand only: new tables and one nullable column.

Revision : 0005
Parents : 0004
Created : 2026-11-11 20:27:06.364168
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "claims",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "public_id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("lang_original", sa.Text(), nullable=False),
        sa.Column("text_original", sa.Text(), nullable=False),
        sa.Column("text_fr", sa.Text(), nullable=False),
        sa.Column("claim_type", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("subject_qid", sa.Text(), nullable=True),
        sa.Column("predicate", sa.Text(), nullable=True),
        sa.Column("value_low", sa.Numeric(), nullable=True),
        sa.Column("value_high", sa.Numeric(), nullable=True),
        sa.Column("unit", sa.Text(), nullable=True),
        sa.Column("unit_scale", sa.Text(), nullable=True),
        sa.Column("currency", sa.Text(), nullable=True),
        sa.Column("value_date", sa.Date(), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("place", sa.Text(), nullable=True),
        sa.Column("polarity", sa.Text(), server_default=sa.text("'affirmative'"), nullable=False),
        sa.Column("invoked_source", sa.Text(), nullable=True),
        sa.Column("definition_variant", sa.Text(), nullable=True),
        sa.Column("comparison_base", sa.Text(), nullable=True),
        sa.Column(
            "jurisdictions",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("primary_jurisdiction", sa.Text(), nullable=True),
        sa.Column(
            "entities",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("theme", sa.Text(), nullable=True),
        sa.Column("verifiability", sa.Float(), nullable=False),
        sa.Column(
            "context_hints",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("source_modality", sa.Text(), server_default=sa.text("'text'"), nullable=False),
        sa.Column("thread_context", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "claim_type IN ('statistic', 'historical_fact', 'quote', 'legal_text', 'ongoing_situation', 'procedure', 'record', 'conduct', 'accusation', 'prediction', 'opinion', 'causal', 'other')",
            name=op.f("ck_claims_claim_type"),
        ),
        sa.CheckConstraint(
            "polarity IN ('affirmative', 'negative')", name=op.f("ck_claims_polarity")
        ),
        sa.CheckConstraint(
            "source_modality IN ('text', 'image', 'video')", name=op.f("ck_claims_source_modality")
        ),
        sa.CheckConstraint("char_length(text_fr) <= 300", name=op.f("ck_claims_text_fr_len")),
        sa.CheckConstraint(
            "char_length(text_original) <= 300", name=op.f("ck_claims_text_original_len")
        ),
        sa.CheckConstraint(
            "verifiability BETWEEN 0 AND 1", name=op.f("ck_claims_verifiability_range")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claims")),
        sa.UniqueConstraint("public_id", name=op.f("uq_claims_public_id")),
    )
    op.create_index(
        "ix_claims_jurisdictions", "claims", ["jurisdictions"], unique=False, postgresql_using="gin"
    )
    op.create_index(
        "ix_claims_primary_jurisdiction", "claims", ["primary_jurisdiction"], unique=False
    )
    op.create_table(
        "source_cache",
        sa.Column("key", sa.LargeBinary(), nullable=False),
        sa.Column("adapter", sa.Text(), nullable=False),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "fetched_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_source_cache")),
    )
    op.create_index(
        op.f("ix_source_cache_expires_at"), "source_cache", ["expires_at"], unique=False
    )
    op.create_table(
        "source_registry",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("publisher", sa.Text(), nullable=False),
        sa.Column("tier", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("country", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("origin", sa.Text(), server_default=sa.text("'seed'"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('institution', 'statistics', 'court', 'parliament', 'agency', 'press', 'encyclopedia', 'fact_checker', 'academic', 'platform', 'other')",
            name=op.f("ck_source_registry_kind"),
        ),
        sa.CheckConstraint(
            "tier IN ('primary', 'secondary', 'tertiary', 'avoid')",
            name=op.f("ck_source_registry_tier"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_registry")),
        sa.UniqueConstraint("domain", name=op.f("uq_source_registry_domain")),
    )
    op.create_table(
        "sources",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("original_url", sa.Text(), nullable=False),
        sa.Column("adapter", sa.Text(), nullable=False),
        sa.Column("stable_id", sa.Text(), nullable=True),
        sa.Column("publisher", sa.Text(), nullable=True),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("tier", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("lang", sa.Text(), nullable=True),
        sa.Column("published_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "retrieved_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sha256", sa.LargeBinary(), nullable=True),
        sa.Column("license", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "tier IN ('primary', 'secondary', 'tertiary', 'avoid')", name=op.f("ck_sources_tier")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sources")),
        sa.UniqueConstraint("canonical_url", name=op.f("uq_sources_canonical_url")),
    )
    op.create_index("ix_sources_domain", "sources", ["domain"], unique=False)
    op.create_table(
        "claim_groups",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "public_id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("representative_claim_id", sa.BigInteger(), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("reach", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("active_verdict_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "level IN ('N0', 'N1', 'N2', 'N3', 'N4')", name=op.f("ck_claim_groups_level")
        ),
        sa.CheckConstraint(
            "state IN ('dormant', 'queued', 'investigating', 'verified', 'to_revalidate', 'passive')",
            name=op.f("ck_claim_groups_state"),
        ),
        sa.ForeignKeyConstraint(
            ["representative_claim_id"],
            ["claims.id"],
            name=op.f("fk_claim_groups_representative_claim_id_claims"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claim_groups")),
        sa.UniqueConstraint("public_id", name=op.f("uq_claim_groups_public_id")),
    )
    op.create_index(
        op.f("ix_claim_groups_representative_claim_id"),
        "claim_groups",
        ["representative_claim_id"],
        unique=False,
    )
    op.create_table(
        "claim_group_members",
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("claim_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "added_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("removed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["claims.id"],
            name=op.f("fk_claim_group_members_claim_id_claims"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["claim_groups.id"],
            name=op.f("fk_claim_group_members_group_id_claim_groups"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("group_id", "claim_id", name=op.f("pk_claim_group_members")),
    )
    op.create_index(
        "uq_claim_group_members_active_claim",
        "claim_group_members",
        ["claim_id"],
        unique=True,
        postgresql_where=sa.text("removed_at IS NULL"),
    )
    op.create_table(
        "content_claims",
        sa.Column("content_id", sa.BigInteger(), nullable=False),
        sa.Column("claim_id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["claims.id"],
            name=op.f("fk_content_claims_claim_id_claims"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["content_id"],
            ["content_items.id"],
            name=op.f("fk_content_claims_content_id_content_items"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("content_id", "claim_id", name=op.f("pk_content_claims")),
    )
    op.create_index(
        op.f("ix_content_claims_claim_id"), "content_claims", ["claim_id"], unique=False
    )
    op.create_table(
        "investigations",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "public_id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("claim_group_id", sa.BigInteger(), nullable=False),
        sa.Column("content_id", sa.BigInteger(), nullable=True),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("depth", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("round", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("cost_usd_micros", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("framing", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("queries", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("judgement", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("second_opinion", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("objection", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("second_pass", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("agreement", sa.Text(), nullable=True),
        sa.Column(
            "flags",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "agreement IN ('identical', 'style', 'substantive')",
            name=op.f("ck_investigations_agreement"),
        ),
        sa.CheckConstraint(
            "depth IN ('standard', 'reinforced')", name=op.f("ck_investigations_depth")
        ),
        sa.CheckConstraint(
            "level IN ('N0', 'N1', 'N2', 'N3', 'N4')", name=op.f("ck_investigations_level")
        ),
        sa.CheckConstraint(
            "stage IN ('framing', 'collecting', 'judging', 'second_opinion', 'objection', 'escalating', 'drafting', 'completed', 'assembled', 'failed')",
            name=op.f("ck_investigations_stage"),
        ),
        sa.ForeignKeyConstraint(
            ["claim_group_id"],
            ["claim_groups.id"],
            name=op.f("fk_investigations_claim_group_id_claim_groups"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["content_id"],
            ["content_items.id"],
            name=op.f("fk_investigations_content_id_content_items"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_investigations")),
        sa.UniqueConstraint("public_id", name=op.f("uq_investigations_public_id")),
    )
    op.create_index("ix_investigations_group", "investigations", ["claim_group_id"], unique=False)
    op.create_index(
        "ix_investigations_stage", "investigations", ["stage", "updated_at"], unique=False
    )
    op.create_table(
        "dossier_claim_groups",
        sa.Column("dossier_id", sa.BigInteger(), nullable=False),
        sa.Column("claim_group_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["claim_group_id"],
            ["claim_groups.id"],
            name=op.f("fk_dossier_claim_groups_claim_group_id_claim_groups"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["dossier_id"],
            ["dossiers.id"],
            name=op.f("fk_dossier_claim_groups_dossier_id_dossiers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "dossier_id", "claim_group_id", name=op.f("pk_dossier_claim_groups")
        ),
    )
    op.create_index(
        op.f("ix_dossier_claim_groups_claim_group_id"),
        "dossier_claim_groups",
        ["claim_group_id"],
        unique=False,
    )
    op.create_table(
        "evidence_passages",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("investigation_id", sa.BigInteger(), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("lang", sa.Text(), nullable=False),
        sa.Column("translation_fr", sa.Text(), nullable=True),
        sa.Column("origin", sa.Text(), nullable=True),
        sa.Column("simhash", sa.BigInteger(), nullable=False),
        sa.Column("score", sa.Float(), server_default=sa.text("0"), nullable=False),
        sa.Column("round", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("kept", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["investigations.id"],
            name=op.f("fk_evidence_passages_investigation_id_investigations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_evidence_passages_source_id_sources"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence_passages")),
        sa.UniqueConstraint(
            "investigation_id", "label", name=op.f("uq_evidence_passages_investigation_id_label")
        ),
    )
    op.create_index(
        op.f("ix_evidence_passages_source_id"), "evidence_passages", ["source_id"], unique=False
    )
    op.create_table(
        "verdicts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "public_id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("claim_group_id", sa.BigInteger(), nullable=False),
        sa.Column("investigation_id", sa.BigInteger(), nullable=True),
        sa.Column("veracity", sa.Text(), nullable=False),
        sa.Column("context_need", sa.Text(), nullable=False),
        sa.Column(
            "context_types",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "rationale",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "not_in_sources",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column(
            "review_points",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column(
            "flags",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "prompt_versions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "route_versions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("drafted_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("notified_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("validated_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("validated_by", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "context_need IN ('none', 'useful', 'necessary')", name=op.f("ck_verdicts_context_need")
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'superseded', 'expired', 'rejected')",
            name=op.f("ck_verdicts_status"),
        ),
        sa.CheckConstraint(
            "veracity IN ('true', 'mostly_true', 'misleading', 'false', 'unverifiable')",
            name=op.f("ck_verdicts_veracity"),
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR confidence BETWEEN 0 AND 1", name=op.f("ck_verdicts_confidence")
        ),
        sa.ForeignKeyConstraint(
            ["claim_group_id"],
            ["claim_groups.id"],
            name=op.f("fk_verdicts_claim_group_id_claim_groups"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["investigations.id"],
            name=op.f("fk_verdicts_investigation_id_investigations"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_verdicts")),
        sa.UniqueConstraint("investigation_id", name=op.f("uq_verdicts_investigation_id")),
        sa.UniqueConstraint("public_id", name=op.f("uq_verdicts_public_id")),
    )
    op.create_index(
        "ix_verdicts_status_drafted", "verdicts", ["status", "drafted_at"], unique=False
    )
    op.create_index(
        "uq_verdicts_active_group",
        "verdicts",
        ["claim_group_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_foreign_key(
        op.f("fk_claim_groups_active_verdict_id_verdicts"),
        "claim_groups",
        "verdicts",
        ["active_verdict_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_table(
        "citations",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("verdict_id", sa.BigInteger(), nullable=False),
        sa.Column("passage_id", sa.BigInteger(), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("assertion", sa.Text(), nullable=False),
        sa.Column("valid", sa.Boolean(), nullable=False),
        sa.Column("position", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.ForeignKeyConstraint(
            ["passage_id"],
            ["evidence_passages.id"],
            name=op.f("fk_citations_passage_id_evidence_passages"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["verdict_id"],
            ["verdicts.id"],
            name=op.f("fk_citations_verdict_id_verdicts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_citations")),
    )
    op.create_index(op.f("ix_citations_passage_id"), "citations", ["passage_id"], unique=False)
    op.create_index(op.f("ix_citations_verdict_id"), "citations", ["verdict_id"], unique=False)
    op.create_table(
        "context_notes",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "public_id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("verdict_id", sa.BigInteger(), nullable=False),
        sa.Column("claim_group_id", sa.BigInteger(), nullable=False),
        sa.Column("context_type", sa.Text(), nullable=True),
        sa.Column("regime", sa.Text(), nullable=False),
        sa.Column("object_type", sa.Text(), nullable=True),
        sa.Column("object_id", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("lang", sa.Text(), nullable=False),
        sa.Column(
            "links",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "checks",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("critic", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "context_type IN ('outdated', 'recycled_media', 'manipulated_media', 'cherry_picking', 'missing_baseline', 'quote_integrity', 'misrepresented_source', 'causation', 'definition', 'scope', 'procedure', 'scientific_uncertainty', 'satire', 'missing_event_context', 'developing')",
            name=op.f("ck_context_notes_context_type"),
        ),
        sa.CheckConstraint(
            "regime IN ('object', 'formulation', 'provisional')",
            name=op.f("ck_context_notes_regime"),
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'validated', 'rejected', 'expired')",
            name=op.f("ck_context_notes_status"),
        ),
        sa.ForeignKeyConstraint(
            ["claim_group_id"],
            ["claim_groups.id"],
            name=op.f("fk_context_notes_claim_group_id_claim_groups"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["verdict_id"],
            ["verdicts.id"],
            name=op.f("fk_context_notes_verdict_id_verdicts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_context_notes")),
        sa.UniqueConstraint("public_id", name=op.f("uq_context_notes_public_id")),
    )
    op.create_index(
        op.f("ix_context_notes_claim_group_id"), "context_notes", ["claim_group_id"], unique=False
    )
    op.create_index(
        op.f("ix_context_notes_verdict_id"), "context_notes", ["verdict_id"], unique=False
    )
    op.create_table(
        "reviews",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("verdict_id", sa.BigInteger(), nullable=False),
        sa.Column("dossier_id", sa.BigInteger(), nullable=True),
        sa.Column("reviewer", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("final_veracity", sa.Text(), nullable=True),
        sa.Column(
            "diff",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "labels",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("review_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision IN ('accept', 'edit', 'reject')", name=op.f("ck_reviews_decision")
        ),
        sa.CheckConstraint(
            "final_veracity IN ('true', 'mostly_true', 'misleading', 'false', 'unverifiable')",
            name=op.f("ck_reviews_final_veracity"),
        ),
        sa.CheckConstraint(
            "outcome IN ('endorsed', 'validated', 'rejected')", name=op.f("ck_reviews_outcome")
        ),
        sa.ForeignKeyConstraint(
            ["dossier_id"],
            ["dossiers.id"],
            name=op.f("fk_reviews_dossier_id_dossiers"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["verdict_id"],
            ["verdicts.id"],
            name=op.f("fk_reviews_verdict_id_verdicts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reviews")),
    )
    op.create_index(op.f("ix_reviews_dossier_id"), "reviews", ["dossier_id"], unique=False)
    op.create_index(op.f("ix_reviews_verdict_id"), "reviews", ["verdict_id"], unique=False)
    op.create_table(
        "context_note_translations",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("note_id", sa.BigInteger(), nullable=False),
        sa.Column("lang", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("fact_sheet", sa.Text(), nullable=True),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("validated_by", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "origin IN ('original', 'human', 'machine')",
            name=op.f("ck_context_note_translations_origin"),
        ),
        sa.ForeignKeyConstraint(
            ["note_id"],
            ["context_notes.id"],
            name=op.f("fk_context_note_translations_note_id_context_notes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_context_note_translations")),
        sa.UniqueConstraint(
            "note_id", "lang", name=op.f("uq_context_note_translations_note_id_lang")
        ),
    )
    op.add_column("dossiers", sa.Column("content_id", sa.BigInteger(), nullable=True))
    op.create_index(op.f("ix_dossiers_content_id"), "dossiers", ["content_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_dossiers_content_id_content_items"),
        "dossiers",
        "content_items",
        ["content_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_dossiers_content_id_content_items"), "dossiers", type_="foreignkey")
    op.drop_index(op.f("ix_dossiers_content_id"), table_name="dossiers")
    op.drop_column("dossiers", "content_id")
    op.drop_table("context_note_translations")
    op.drop_index(op.f("ix_reviews_verdict_id"), table_name="reviews")
    op.drop_index(op.f("ix_reviews_dossier_id"), table_name="reviews")
    op.drop_table("reviews")
    op.drop_index(op.f("ix_context_notes_verdict_id"), table_name="context_notes")
    op.drop_index(op.f("ix_context_notes_claim_group_id"), table_name="context_notes")
    op.drop_table("context_notes")
    op.drop_index(op.f("ix_citations_verdict_id"), table_name="citations")
    op.drop_index(op.f("ix_citations_passage_id"), table_name="citations")
    op.drop_table("citations")
    op.drop_constraint(
        op.f("fk_claim_groups_active_verdict_id_verdicts"), "claim_groups", type_="foreignkey"
    )
    op.drop_index(
        "uq_verdicts_active_group",
        table_name="verdicts",
        postgresql_where=sa.text("status = 'active'"),
    )
    op.drop_index("ix_verdicts_status_drafted", table_name="verdicts")
    op.drop_table("verdicts")
    op.drop_index(op.f("ix_evidence_passages_source_id"), table_name="evidence_passages")
    op.drop_table("evidence_passages")
    op.drop_index(op.f("ix_dossier_claim_groups_claim_group_id"), table_name="dossier_claim_groups")
    op.drop_table("dossier_claim_groups")
    op.drop_index("ix_investigations_stage", table_name="investigations")
    op.drop_index("ix_investigations_group", table_name="investigations")
    op.drop_table("investigations")
    op.drop_index(op.f("ix_content_claims_claim_id"), table_name="content_claims")
    op.drop_table("content_claims")
    op.drop_index(
        "uq_claim_group_members_active_claim",
        table_name="claim_group_members",
        postgresql_where=sa.text("removed_at IS NULL"),
    )
    op.drop_table("claim_group_members")
    op.drop_index(op.f("ix_claim_groups_representative_claim_id"), table_name="claim_groups")
    op.drop_table("claim_groups")
    op.drop_index("ix_sources_domain", table_name="sources")
    op.drop_table("sources")
    op.drop_table("source_registry")
    op.drop_index(op.f("ix_source_cache_expires_at"), table_name="source_cache")
    op.drop_table("source_cache")
    op.drop_index("ix_claims_primary_jurisdiction", table_name="claims")
    op.drop_index("ix_claims_jurisdictions", table_name="claims", postgresql_using="gin")
    op.drop_table("claims")
