"""Fact-check V1 (lot 4): pgvector, post embeddings (weekly partitions, no vector
index), group journal, reference facts, value bands, processing profiles, level
overrides, E1 router models; vectors on claims and groups (HNSW on the group
representative only), reuse columns on verdicts.

Expand only: new tables, nullable columns, new CHECK constraints on new columns and
the group state ``merged`` added to its CHECK (widening, same name). The lot 3 code
runs unchanged on this schema.

Requires pgvector >= 0.8 (``halfvec``). On Railway the standard PostgreSQL image does
not ship it: use the pgvector template (docs/deployment.md).

Revision : 0006
Parents : 0005
Created : 2026-11-11 22:37:09.400001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from tower.core.db.vector import HalfVec

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OLD_STATES = "'dormant', 'queued', 'investigating', 'verified', 'to_revalidate', 'passive'"


def _require_pgvector() -> None:
    op.execute(
        """
        DO $$
        DECLARE
            available text;
        BEGIN
            SELECT default_version INTO available
            FROM pg_available_extensions WHERE name = 'vector';
            IF available IS NULL THEN
                RAISE EXCEPTION 'TWR-FCK-035: PostgreSQL extension pgvector is not available '
                    'on this server (needs pgvector >= 0.8; on Railway use the pgvector '
                    'template)';
            END IF;
            IF string_to_array(available, '.')::int[] < ARRAY[0, 8] THEN
                RAISE EXCEPTION 'TWR-FCK-035: pgvector % is too old (needs >= 0.8)', available;
            END IF;
        END $$;
        """
    )
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def upgrade() -> None:
    _require_pgvector()
    op.create_table(
        "level_overrides",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "public_id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_id", sa.BigInteger(), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("author", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("revoked_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "level IN ('N0', 'N1', 'N2', 'N3', 'N4')", name=op.f("ck_level_overrides_level")
        ),
        sa.CheckConstraint(
            "target_type IN ('content', 'group', 'account')",
            name=op.f("ck_level_overrides_target_type"),
        ),
        sa.CheckConstraint(
            "char_length(reason) > 0", name=op.f("ck_level_overrides_reason_required")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_level_overrides")),
        sa.UniqueConstraint("public_id", name=op.f("uq_level_overrides_public_id")),
    )
    op.create_index(
        "ix_level_overrides_target",
        "level_overrides",
        ["target_type", "target_id", "expires_at"],
        unique=False,
    )
    op.create_table(
        "processing_profiles",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("based_on_version", sa.Integer(), nullable=True),
        sa.Column("author", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("activated_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("activated_by", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_processing_profiles")),
        sa.UniqueConstraint("version", name=op.f("uq_processing_profiles_version")),
    )
    op.create_index(
        "uq_processing_profiles_active",
        "processing_profiles",
        ["active"],
        unique=True,
        postgresql_where=sa.text("active"),
    )
    op.create_table(
        "reference_facts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "public_id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("indicator", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("definition_variant", sa.Text(), nullable=True),
        sa.Column("subject_qid", sa.Text(), nullable=True),
        sa.Column(
            "aliases",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("value", sa.Numeric(), nullable=False),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("unit_scale", sa.Text(), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("territory", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("published_at", sa.Date(), nullable=False),
        sa.Column("next_publication", sa.Date(), nullable=True),
        sa.Column("previous_fact_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("origin_verdict_id", sa.BigInteger(), nullable=True),
        sa.Column("external_key", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("validated_by", sa.Text(), nullable=True),
        sa.Column("validated_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
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
            "origin IN ('investigation', 'insee', 'manual')", name=op.f("ck_reference_facts_origin")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'validated', 'rejected', 'superseded')",
            name=op.f("ck_reference_facts_status"),
        ),
        sa.CheckConstraint(
            "period_start <= period_end", name=op.f("ck_reference_facts_period_order")
        ),
        sa.ForeignKeyConstraint(
            ["origin_verdict_id"],
            ["verdicts.id"],
            name=op.f("fk_reference_facts_origin_verdict_id_verdicts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["previous_fact_id"],
            ["reference_facts.id"],
            name=op.f("fk_reference_facts_previous_fact_id_reference_facts"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reference_facts")),
        sa.UniqueConstraint("external_key", name=op.f("uq_reference_facts_external_key")),
        sa.UniqueConstraint("public_id", name=op.f("uq_reference_facts_public_id")),
    )
    op.create_index(
        "ix_reference_facts_aliases",
        "reference_facts",
        ["aliases"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_index(
        "ix_reference_facts_lookup",
        "reference_facts",
        ["indicator", "territory", "period_end"],
        unique=False,
    )
    op.create_index(
        "ix_reference_facts_subject_qid", "reference_facts", ["subject_qid"], unique=False
    )
    op.create_table(
        "router_models",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("embedding_model", sa.Text(), nullable=False),
        sa.Column("dims", sa.Integer(), nullable=False),
        sa.Column("weights", postgresql.ARRAY(sa.DOUBLE_PRECISION()), nullable=False),
        sa.Column("bias", sa.DOUBLE_PRECISION(), nullable=False),
        sa.Column(
            "metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("sample_size", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "trained_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("activated_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("activated_by", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "cardinality(weights) = dims", name=op.f("ck_router_models_weights_dims")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_router_models")),
        sa.UniqueConstraint("version", name=op.f("uq_router_models_version")),
    )
    op.create_index(
        "uq_router_models_active",
        "router_models",
        ["active"],
        unique=True,
        postgresql_where=sa.text("active"),
    )
    op.create_table(
        "claim_group_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("other_group_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "claim_ids",
            postgresql.ARRAY(sa.BigInteger()),
            server_default=sa.text("'{}'::bigint[]"),
            nullable=False,
        ),
        sa.Column("from_level", sa.Text(), nullable=True),
        sa.Column("to_level", sa.Text(), nullable=True),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "data",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('created', 'joined', 'merged', 'split', 'level_changed', 'representative_changed', 'fact_linked')",
            name=op.f("ck_claim_group_events_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["claim_groups.id"],
            name=op.f("fk_claim_group_events_group_id_claim_groups"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["other_group_id"],
            ["claim_groups.id"],
            name=op.f("fk_claim_group_events_other_group_id_claim_groups"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claim_group_events")),
    )
    op.create_index(
        "ix_claim_group_events_group",
        "claim_group_events",
        ["group_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "verdict_bands",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "public_id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("reference_fact_id", sa.BigInteger(), nullable=False),
        sa.Column("band", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("veracity", sa.Text(), nullable=False),
        sa.Column("context_need", sa.Text(), nullable=False),
        sa.Column(
            "context_types",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("source_verdict_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("validated_by", sa.Text(), nullable=False),
        sa.Column(
            "validated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_by", sa.Text(), nullable=True),
        sa.Column("revoked_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "band IN ('contains', 'within_1_tau', 'within_3_tau', 'beyond')",
            name=op.f("ck_verdict_bands_band"),
        ),
        sa.CheckConstraint(
            "context_need IN ('none', 'useful', 'necessary')",
            name=op.f("ck_verdict_bands_context_need"),
        ),
        sa.CheckConstraint(
            "side IN ('none', 'above', 'below')", name=op.f("ck_verdict_bands_side")
        ),
        sa.CheckConstraint("status IN ('active', 'revoked')", name=op.f("ck_verdict_bands_status")),
        sa.CheckConstraint(
            "veracity IN ('true', 'mostly_true', 'misleading', 'false', 'unverifiable')",
            name=op.f("ck_verdict_bands_veracity"),
        ),
        sa.ForeignKeyConstraint(
            ["reference_fact_id"],
            ["reference_facts.id"],
            name=op.f("fk_verdict_bands_reference_fact_id_reference_facts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_verdict_id"],
            ["verdicts.id"],
            name=op.f("fk_verdict_bands_source_verdict_id_verdicts"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_verdict_bands")),
        sa.UniqueConstraint("public_id", name=op.f("uq_verdict_bands_public_id")),
    )
    op.create_index(
        "uq_verdict_bands_active",
        "verdict_bands",
        ["reference_fact_id", "band", "side"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "content_embeddings",
        sa.Column("content_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("embedding", HalfVec(512), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("lang", sa.Text(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("sampled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("router_version", sa.Integer(), nullable=True),
        sa.CheckConstraint("level IN ('N0', 'N1')", name=op.f("ck_content_embeddings_level")),
        sa.CheckConstraint(
            "score IS NULL OR score BETWEEN 0 AND 1", name=op.f("ck_content_embeddings_score_range")
        ),
        sa.ForeignKeyConstraint(
            ["content_id"],
            ["content_items.id"],
            name=op.f("fk_content_embeddings_content_id_content_items"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("content_id", "created_at", name=op.f("pk_content_embeddings")),
        postgresql_partition_by="RANGE (created_at)",
    )
    op.create_index(
        op.f("ix_content_embeddings_content_id"), "content_embeddings", ["content_id"], unique=False
    )
    # Weekly partitions (Monday 00:00 UTC): previous week to two weeks ahead; the
    # factcheck service creates the next ones and drops those older than 14 days.
    op.execute(
        """
        DO $$
        DECLARE
            week date := date_trunc('week', now() AT TIME ZONE 'UTC')::date - 7;
        BEGIN
            FOR i IN 0..3 LOOP
                EXECUTE format(
                    'CREATE TABLE IF NOT EXISTS %I PARTITION OF content_embeddings '
                    'FOR VALUES FROM (%L) TO (%L)',
                    'content_embeddings_p' || to_char(week, 'YYYYMMDD'),
                    week::timestamp AT TIME ZONE 'UTC',
                    (week + 7)::timestamp AT TIME ZONE 'UTC'
                );
                week := week + 7;
            END LOOP;
        END $$;
        """
    )
    op.add_column(
        "claim_groups", sa.Column("representative_embedding", HalfVec(512), nullable=True)
    )
    op.add_column("claim_groups", sa.Column("reference_fact_id", sa.BigInteger(), nullable=True))
    op.add_column("claim_groups", sa.Column("band", sa.Text(), nullable=True))
    op.add_column("claim_groups", sa.Column("band_side", sa.Text(), nullable=True))
    op.add_column("claim_groups", sa.Column("merged_into_id", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        "band", "claim_groups", "band IN ('contains', 'within_1_tau', 'within_3_tau', 'beyond')"
    )
    op.create_check_constraint(
        "band_side", "claim_groups", "band_side IN ('none', 'above', 'below')"
    )
    # Widening (same name): the lot 3 code never writes ``merged``.
    op.drop_constraint(op.f("ck_claim_groups_state"), "claim_groups", type_="check")
    op.create_check_constraint("state", "claim_groups", f"state IN ({_OLD_STATES}, 'merged')")
    op.create_index(
        "ix_claim_groups_reference_fact", "claim_groups", ["reference_fact_id"], unique=False
    )
    op.create_index(
        "ix_claim_groups_representative_embedding",
        "claim_groups",
        ["representative_embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"representative_embedding": "halfvec_cosine_ops"},
    )
    op.create_foreign_key(
        op.f("fk_claim_groups_reference_fact_id_reference_facts"),
        "claim_groups",
        "reference_facts",
        ["reference_fact_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        op.f("fk_claim_groups_merged_into_id_claim_groups"),
        "claim_groups",
        "claim_groups",
        ["merged_into_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column("claims", sa.Column("embedding", HalfVec(512), nullable=True))
    op.add_column("claims", sa.Column("embedding_pivot", HalfVec(512), nullable=True))
    op.add_column("claims", sa.Column("embedding_model", sa.Text(), nullable=True))
    op.add_column("verdicts", sa.Column("reused_from_verdict_id", sa.BigInteger(), nullable=True))
    op.add_column("verdicts", sa.Column("reuse_rule", sa.Text(), nullable=True))
    op.create_check_constraint(
        "reuse_rule", "verdicts", "reuse_rule IN ('identical_signature', 'band')"
    )
    op.create_foreign_key(
        op.f("fk_verdicts_reused_from_verdict_id_verdicts"),
        "verdicts",
        "verdicts",
        ["reused_from_verdict_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_verdicts_reuse_rule"), "verdicts", type_="check")
    op.drop_constraint(
        op.f("fk_verdicts_reused_from_verdict_id_verdicts"), "verdicts", type_="foreignkey"
    )
    op.drop_column("verdicts", "reuse_rule")
    op.drop_column("verdicts", "reused_from_verdict_id")
    op.drop_column("claims", "embedding_model")
    op.drop_column("claims", "embedding_pivot")
    op.drop_column("claims", "embedding")
    op.drop_constraint(
        op.f("fk_claim_groups_merged_into_id_claim_groups"), "claim_groups", type_="foreignkey"
    )
    op.drop_constraint(
        op.f("fk_claim_groups_reference_fact_id_reference_facts"),
        "claim_groups",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_claim_groups_representative_embedding",
        table_name="claim_groups",
        postgresql_using="hnsw",
        postgresql_ops={"representative_embedding": "halfvec_cosine_ops"},
    )
    op.drop_index("ix_claim_groups_reference_fact", table_name="claim_groups")
    op.execute("UPDATE claim_groups SET state = 'passive' WHERE state = 'merged'")
    op.drop_constraint(op.f("ck_claim_groups_state"), "claim_groups", type_="check")
    op.create_check_constraint("state", "claim_groups", f"state IN ({_OLD_STATES})")
    op.drop_constraint(op.f("ck_claim_groups_band_side"), "claim_groups", type_="check")
    op.drop_constraint(op.f("ck_claim_groups_band"), "claim_groups", type_="check")
    op.drop_column("claim_groups", "merged_into_id")
    op.drop_column("claim_groups", "band_side")
    op.drop_column("claim_groups", "band")
    op.drop_column("claim_groups", "reference_fact_id")
    op.drop_column("claim_groups", "representative_embedding")
    op.drop_index(op.f("ix_content_embeddings_content_id"), table_name="content_embeddings")
    op.drop_table("content_embeddings")
    op.drop_index(
        "uq_verdict_bands_active",
        table_name="verdict_bands",
        postgresql_where=sa.text("status = 'active'"),
    )
    op.drop_table("verdict_bands")
    op.drop_index("ix_claim_group_events_group", table_name="claim_group_events")
    op.drop_table("claim_group_events")
    op.drop_index(
        "uq_router_models_active", table_name="router_models", postgresql_where=sa.text("active")
    )
    op.drop_table("router_models")
    op.drop_index("ix_reference_facts_subject_qid", table_name="reference_facts")
    op.drop_index("ix_reference_facts_lookup", table_name="reference_facts")
    op.drop_index(
        "ix_reference_facts_aliases", table_name="reference_facts", postgresql_using="gin"
    )
    op.drop_table("reference_facts")
    op.drop_index(
        "uq_processing_profiles_active",
        table_name="processing_profiles",
        postgresql_where=sa.text("active"),
    )
    op.drop_table("processing_profiles")
    op.drop_index("ix_level_overrides_target", table_name="level_overrides")
    op.drop_table("level_overrides")
    # The extension is left installed: it may serve other databases objects and
    # dropping it would need superuser rights on managed hosts.
