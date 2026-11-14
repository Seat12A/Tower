"""Fact-check V2 (lot 5): E2 extraction tracking and micro-batches, extraction audits,
scheduler runs, narratives (groups, journal, window stats, novelty), entity rarity;
scheduler and light-check columns on claim groups.

Expand only: new tables, new columns (nullable, or NOT NULL with a constant default,
which PostgreSQL adds without rewriting the table), a CHECK on a new column. The lot 4
code runs unchanged on this schema.

Revision : 0007
Parents : 0006
Created : 2026-11-12 12:24:02.579975
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "entity_rarity",
        sa.Column("entity_key", sa.Text(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("entity_key", "day", name=op.f("pk_entity_rarity")),
    )
    op.create_table(
        "extraction_batches",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("content_ids", postgresql.ARRAY(sa.BigInteger()), nullable=False),
        sa.Column("canary_token", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "fidelity_failures", sa.SmallInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("contamination", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("cost_usd_micros", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'done', 'replayed_solo', 'failed')",
            name=op.f("ck_extraction_batches_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_extraction_batches")),
    )
    op.create_index(
        "ix_extraction_batches_created", "extraction_batches", ["created_at"], unique=False
    )
    op.create_table(
        "scheduler_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "started_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("rate_per_hour", sa.Float(), nullable=False),
        sa.Column("interval_s", sa.Integer(), nullable=False),
        sa.Column(
            "stats",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scheduler_runs")),
    )
    op.create_index("ix_scheduler_runs_started", "scheduler_runs", ["started_at"], unique=False)
    op.create_table(
        "narratives",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "public_id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("title_status", sa.Text(), server_default=sa.text("'none'"), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("theme", sa.Text(), nullable=True),
        sa.Column("founder_group_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "medoid_group_ids",
            postgresql.ARRAY(sa.BigInteger()),
            server_default=sa.text("'{}'::bigint[]"),
            nullable=False,
        ),
        sa.Column("medoids_elected_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("previous_narrative_id", sa.BigInteger(), nullable=True),
        sa.Column("merged_into_id", sa.BigInteger(), nullable=True),
        sa.Column("representative_group_id", sa.BigInteger(), nullable=True),
        sa.Column("split_suggested", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("state_changed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("surge_started_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("below_watch_since", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "opened_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_activity_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("closed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
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
            "state IN ('active', 'watch', 'surge', 'alert', 'closed')",
            name=op.f("ck_narratives_state"),
        ),
        sa.CheckConstraint(
            "title_status IN ('none', 'draft', 'validated')",
            name=op.f("ck_narratives_title_status"),
        ),
        sa.CheckConstraint(
            "cardinality(medoid_group_ids) <= 3", name=op.f("ck_narratives_medoids_max")
        ),
        sa.ForeignKeyConstraint(
            ["founder_group_id"],
            ["claim_groups.id"],
            name=op.f("fk_narratives_founder_group_id_claim_groups"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["merged_into_id"],
            ["narratives.id"],
            name=op.f("fk_narratives_merged_into_id_narratives"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["previous_narrative_id"],
            ["narratives.id"],
            name=op.f("fk_narratives_previous_narrative_id_narratives"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["representative_group_id"],
            ["claim_groups.id"],
            name=op.f("fk_narratives_representative_group_id_claim_groups"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_narratives")),
        sa.UniqueConstraint("public_id", name=op.f("uq_narratives_public_id")),
    )
    op.create_index(
        "ix_narratives_state_activity", "narratives", ["state", "last_activity_at"], unique=False
    )
    op.create_table(
        "narrative_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("narrative_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("other_narrative_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "group_ids",
            postgresql.ARRAY(sa.BigInteger()),
            server_default=sa.text("'{}'::bigint[]"),
            nullable=False,
        ),
        sa.Column("from_state", sa.Text(), nullable=True),
        sa.Column("to_state", sa.Text(), nullable=True),
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
            "kind IN ('created', 'attached', 'detached', 'merged', 'split', 'state_changed', 'medoids_changed', 'title_set', 'closed')",
            name=op.f("ck_narrative_events_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["narrative_id"],
            ["narratives.id"],
            name=op.f("fk_narrative_events_narrative_id_narratives"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["other_narrative_id"],
            ["narratives.id"],
            name=op.f("fk_narrative_events_other_narrative_id_narratives"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_narrative_events")),
    )
    op.create_index(
        "ix_narrative_events_narrative",
        "narrative_events",
        ["narrative_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "narrative_groups",
        sa.Column("narrative_id", sa.BigInteger(), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "added_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("removed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "signals",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["claim_groups.id"],
            name=op.f("fk_narrative_groups_group_id_claim_groups"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["narrative_id"],
            ["narratives.id"],
            name=op.f("fk_narrative_groups_narrative_id_narratives"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "narrative_id", "group_id", "added_at", name=op.f("pk_narrative_groups")
        ),
    )
    op.create_index(
        "uq_narrative_groups_active_group",
        "narrative_groups",
        ["group_id"],
        unique=True,
        postgresql_where=sa.text("removed_at IS NULL"),
    )
    op.create_table(
        "narrative_stats",
        sa.Column("narrative_id", sa.BigInteger(), nullable=False),
        sa.Column("span", sa.Text(), nullable=False),
        sa.Column("window_end", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("posts", sa.Integer(), nullable=False),
        sa.Column("flow_posts", sa.Integer(), nullable=False),
        sa.Column("share", sa.Float(), nullable=False),
        sa.Column("accounts", sa.Integer(), nullable=False),
        sa.Column("platforms", sa.SmallInteger(), nullable=False),
        sa.Column("reach", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.CheckConstraint("span IN ('5m', '1h', '6h')", name=op.f("ck_narrative_stats_span")),
        sa.ForeignKeyConstraint(
            ["narrative_id"],
            ["narratives.id"],
            name=op.f("fk_narrative_stats_narrative_id_narratives"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "narrative_id", "span", "window_end", name=op.f("pk_narrative_stats")
        ),
    )
    op.create_index(
        "ix_narrative_stats_window_end", "narrative_stats", ["window_end"], unique=False
    )
    op.create_table(
        "content_extractions",
        sa.Column("content_id", sa.BigInteger(), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("batch_id", sa.BigInteger(), nullable=True),
        sa.Column("position", sa.SmallInteger(), nullable=True),
        sa.Column("narrative_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('extracted', 'no_claim', 'sampled_out', 'failed')",
            name=op.f("ck_content_extractions_status"),
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["extraction_batches.id"],
            name=op.f("fk_content_extractions_batch_id_extraction_batches"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["content_id"],
            ["content_items.id"],
            name=op.f("fk_content_extractions_content_id_content_items"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["narrative_id"],
            ["narratives.id"],
            name=op.f("fk_content_extractions_narrative_id_narratives"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("content_id", name=op.f("pk_content_extractions")),
    )
    op.create_index(
        op.f("ix_content_extractions_batch_id"), "content_extractions", ["batch_id"], unique=False
    )
    op.create_index(
        "ix_content_extractions_created", "content_extractions", ["created_at"], unique=False
    )
    op.create_table(
        "extraction_audits",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("content_id", sa.BigInteger(), nullable=False),
        sa.Column("batch_id", sa.BigInteger(), nullable=True),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("batch_size", sa.SmallInteger(), nullable=False),
        sa.Column("batch_claims", sa.SmallInteger(), nullable=False),
        sa.Column("solo_claims", sa.SmallInteger(), nullable=False),
        sa.Column("discordant", sa.Boolean(), nullable=False),
        sa.Column(
            "details",
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
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["extraction_batches.id"],
            name=op.f("fk_extraction_audits_batch_id_extraction_batches"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["content_id"],
            ["content_items.id"],
            name=op.f("fk_extraction_audits_content_id_content_items"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_extraction_audits")),
    )
    op.create_index(
        op.f("ix_extraction_audits_content_id"), "extraction_audits", ["content_id"], unique=False
    )
    op.create_table(
        "narrative_novelty",
        sa.Column("narrative_id", sa.BigInteger(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("posts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("accounts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "account_ids",
            postgresql.ARRAY(sa.BigInteger()),
            server_default=sa.text("'{}'::bigint[]"),
            nullable=False,
        ),
        sa.Column("best_content_id", sa.BigInteger(), nullable=True),
        sa.Column("best_engagement", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("forced_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "first_seen_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('number', 'link', 'mention', 'media', 'entity')",
            name=op.f("ck_narrative_novelty_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["best_content_id"],
            ["content_items.id"],
            name=op.f("fk_narrative_novelty_best_content_id_content_items"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["narrative_id"],
            ["narratives.id"],
            name=op.f("fk_narrative_novelty_narrative_id_narratives"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("narrative_id", "fingerprint", name=op.f("pk_narrative_novelty")),
    )
    op.add_column("claim_groups", sa.Column("light_signal", sa.Text(), nullable=True))
    op.add_column(
        "claim_groups",
        sa.Column("light_checked_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "claim_groups",
        sa.Column("light_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "claim_groups",
        sa.Column("observers_6h", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "claim_groups",
        sa.Column("velocity", sa.Float(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "claim_groups",
        sa.Column("priority", sa.Float(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "claim_groups",
        sa.Column("level_changed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "claim_groups",
        sa.Column("below_threshold_since", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index("ix_claim_groups_level_state", "claim_groups", ["level", "state"], unique=False)
    op.create_check_constraint(
        "light_signal",
        "claim_groups",
        "light_signal IN ('likely_true', 'likely_false', 'uncertain', 'needs_context')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_claim_groups_light_signal"), "claim_groups", type_="check")
    op.drop_index("ix_claim_groups_level_state", table_name="claim_groups")
    op.drop_column("claim_groups", "below_threshold_since")
    op.drop_column("claim_groups", "level_changed_at")
    op.drop_column("claim_groups", "priority")
    op.drop_column("claim_groups", "velocity")
    op.drop_column("claim_groups", "observers_6h")
    op.drop_column("claim_groups", "light_evidence")
    op.drop_column("claim_groups", "light_checked_at")
    op.drop_column("claim_groups", "light_signal")
    op.drop_table("narrative_novelty")
    op.drop_index(op.f("ix_extraction_audits_content_id"), table_name="extraction_audits")
    op.drop_table("extraction_audits")
    op.drop_index("ix_content_extractions_created", table_name="content_extractions")
    op.drop_index(op.f("ix_content_extractions_batch_id"), table_name="content_extractions")
    op.drop_table("content_extractions")
    op.drop_index("ix_narrative_stats_window_end", table_name="narrative_stats")
    op.drop_table("narrative_stats")
    op.drop_index(
        "uq_narrative_groups_active_group",
        table_name="narrative_groups",
        postgresql_where=sa.text("removed_at IS NULL"),
    )
    op.drop_table("narrative_groups")
    op.drop_index("ix_narrative_events_narrative", table_name="narrative_events")
    op.drop_table("narrative_events")
    op.drop_index("ix_narratives_state_activity", table_name="narratives")
    op.drop_table("narratives")
    op.drop_index("ix_scheduler_runs_started", table_name="scheduler_runs")
    op.drop_table("scheduler_runs")
    op.drop_index("ix_extraction_batches_created", table_name="extraction_batches")
    op.drop_table("extraction_batches")
    op.drop_table("entity_rarity")
