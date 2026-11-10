"""Socle initial : dossiers, outbox, jobs, auth, erreurs.

Révision : 0001
Parent :
Créée le : 2026-11-10 19:07:53.755139
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from tower.core.db.types import XID8

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dossiers",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "public_id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("x_post_id", sa.BigInteger(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("priority", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("assigned_to", sa.BigInteger(), nullable=True),
        sa.Column("published_url", sa.Text(), nullable=True),
        sa.Column("opened_by", sa.Text(), nullable=False),
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
        sa.CheckConstraint("kind IN ('fact_check', 'moderation')", name=op.f("ck_dossiers_kind")),
        sa.CheckConstraint(
            "status IN ('received', 'in_review', 'validated', 'published', 'rejected')",
            name=op.f("ck_dossiers_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dossiers")),
        sa.UniqueConstraint("public_id", name=op.f("uq_dossiers_public_id")),
    )
    op.create_index(
        "ix_dossiers_status_kind_created",
        "dossiers",
        ["status", "kind", "created_at"],
        unique=False,
    )
    op.create_index(op.f("ix_dossiers_x_post_id"), "dossiers", ["x_post_id"], unique=False)
    op.create_table(
        "error_reports",
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("process", sa.Text(), nullable=False),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("exception_type", sa.Text(), nullable=True),
        sa.Column("last_message", sa.Text(), nullable=True),
        sa.Column("last_traceback", sa.Text(), nullable=True),
        sa.Column(
            "last_context",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("occurrences", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "first_seen",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("resolved_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.Text(), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "severity IN ('info', 'warning', 'error', 'critical')",
            name=op.f("ck_error_reports_severity"),
        ),
        sa.PrimaryKeyConstraint("fingerprint", name=op.f("pk_error_reports")),
    )
    op.create_index("ix_error_reports_code", "error_reports", ["code"], unique=False)
    op.create_index(
        "ix_error_reports_open",
        "error_reports",
        ["last_seen"],
        unique=False,
        postgresql_where=sa.text("resolved_at IS NULL"),
    )
    op.create_table(
        "event_cursors",
        sa.Column("consumer", sa.Text(), nullable=False),
        sa.Column("last_xid", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_id", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("consumer", name=op.f("pk_event_cursors")),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("xid", XID8(), server_default=sa.text("pg_current_xact_id()"), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("aggregate_type", sa.Text(), nullable=True),
        sa.Column("aggregate_id", sa.Text(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("actor", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
    )
    op.create_index("ix_events_created_at", "events", ["created_at"], unique=False)
    op.create_index("ix_events_xid_id", "events", ["xid", "id"], unique=False)
    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("queue", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("priority", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("5"), nullable=False),
        sa.Column(
            "run_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("locked_by", sa.Text(), nullable=True),
        sa.Column("locked_until", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("dedupe_key", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name=op.f("ck_jobs_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    op.create_index(
        "ix_jobs_finished",
        "jobs",
        ["finished_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('succeeded', 'failed', 'cancelled')"),
    )
    op.create_index(
        "ix_jobs_lease",
        "jobs",
        ["locked_until"],
        unique=False,
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index(
        "ix_jobs_ready",
        "jobs",
        ["queue", sa.literal_column("priority DESC"), "run_at", "id"],
        unique=False,
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "uq_jobs_dedupe_active",
        "jobs",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_table(
        "service_clients",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column(
            "scopes", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_service_clients")),
        sa.UniqueConstraint("name", name=op.f("uq_service_clients_name")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_service_clients_token_hash")),
    )
    op.create_table(
        "staff_members",
        sa.Column("discord_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column(
            "roles", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.PrimaryKeyConstraint("discord_id", name=op.f("pk_staff_members")),
    )
    op.create_table(
        "dossier_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("dossier_id", sa.BigInteger(), nullable=False),
        sa.Column("from_status", sa.Text(), nullable=True),
        sa.Column("to_status", sa.Text(), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["dossier_id"],
            ["dossiers.id"],
            name=op.f("fk_dossier_events_dossier_id_dossiers"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dossier_events")),
    )
    op.create_index(
        op.f("ix_dossier_events_dossier_id"), "dossier_events", ["dossier_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_dossier_events_dossier_id"), table_name="dossier_events")
    op.drop_table("dossier_events")
    op.drop_table("staff_members")
    op.drop_table("service_clients")
    op.drop_index(
        "uq_jobs_dedupe_active",
        table_name="jobs",
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.drop_index("ix_jobs_ready", table_name="jobs", postgresql_where=sa.text("status = 'queued'"))
    op.drop_index(
        "ix_jobs_lease", table_name="jobs", postgresql_where=sa.text("status = 'running'")
    )
    op.drop_index(
        "ix_jobs_finished",
        table_name="jobs",
        postgresql_where=sa.text("status IN ('succeeded', 'failed', 'cancelled')"),
    )
    op.drop_table("jobs")
    op.drop_index("ix_events_xid_id", table_name="events")
    op.drop_index("ix_events_created_at", table_name="events")
    op.drop_table("events")
    op.drop_table("event_cursors")
    op.drop_index(
        "ix_error_reports_open",
        table_name="error_reports",
        postgresql_where=sa.text("resolved_at IS NULL"),
    )
    op.drop_index("ix_error_reports_code", table_name="error_reports")
    op.drop_table("error_reports")
    op.drop_index(op.f("ix_dossiers_x_post_id"), table_name="dossiers")
    op.drop_index("ix_dossiers_status_kind_created", table_name="dossiers")
    op.drop_table("dossiers")
