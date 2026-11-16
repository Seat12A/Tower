"""Seat 12A verification badges: badges, affiliations, history, requests, reports,
organisation keys, Wikidata links and import runs, X verification checks.

Expand only: new tables, no change to existing ones. The deployed code ignores them.

Revision : 0013
Parents : 0012
Created : 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "badge_events",
    "badge_requests",
    "badge_reports",
    "badge_org_keys",
    "badge_wikidata_links",
    "badge_x_checks",
    "badge_import_runs",
    "badge_affiliations",
    "badges",
)


def upgrade() -> None:
    op.create_table(
        "badge_import_runs",
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("started_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("listed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("accounts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("batches", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("scope", name=op.f("pk_badge_import_runs")),
    )
    op.create_table(
        "badge_reports",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "public_id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'open'"), nullable=False),
        sa.Column("resolved_by", sa.Text(), nullable=True),
        sa.Column("resolved_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "reason IN ('wrong_category', 'not_eligible', 'impersonation', 'wrong_affiliation', 'other')",
            name=op.f("ck_badge_reports_reason"),
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved', 'dismissed')", name=op.f("ck_badge_reports_status")
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["platform_accounts.id"],
            name=op.f("fk_badge_reports_account_id_platform_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_badge_reports")),
        sa.UniqueConstraint("public_id", name=op.f("uq_badge_reports_public_id")),
    )
    op.create_index("ix_badge_reports_account_id", "badge_reports", ["account_id"], unique=False)
    op.create_index(
        "ix_badge_reports_status_created", "badge_reports", ["status", "created_at"], unique=False
    )
    op.create_table(
        "badge_wikidata_links",
        sa.Column("member_account_id", sa.BigInteger(), nullable=False),
        sa.Column("org_qid", sa.Text(), nullable=False),
        sa.Column("property", sa.Text(), nullable=False),
        sa.Column(
            "seen_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["member_account_id"],
            ["platform_accounts.id"],
            name=op.f("fk_badge_wikidata_links_member_account_id_platform_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "member_account_id", "org_qid", "property", name=op.f("pk_badge_wikidata_links")
        ),
    )
    op.create_index(
        "ix_badge_wikidata_links_org_qid", "badge_wikidata_links", ["org_qid"], unique=False
    )
    op.create_table(
        "badge_x_checks",
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("hinted_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("checked_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("verified_type", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["platform_accounts.id"],
            name=op.f("fk_badge_x_checks_account_id_platform_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("account_id", name=op.f("pk_badge_x_checks")),
    )
    op.create_index(
        "ix_badge_x_checks_due",
        "badge_x_checks",
        ["hinted_at"],
        unique=False,
        postgresql_where=sa.text("checked_at IS NULL"),
    )
    op.create_table(
        "badges",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "public_id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("logo_url", sa.Text(), nullable=True),
        sa.Column("wikidata_qid", sa.Text(), nullable=True),
        sa.Column("wikidata_seen_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("granted_by", sa.Text(), nullable=True),
        sa.Column("granted_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("ended_by", sa.Text(), nullable=True),
        sa.Column("ended_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("end_reason", sa.Text(), nullable=True),
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
            "category IS NULL OR category IN ('institutional', 'organization', 'media', 'expert', 'personality')",
            name=op.f("ck_badges_category"),
        ),
        sa.CheckConstraint(
            "source IS NULL OR source IN ('manual', 'request', 'wikidata', 'x_government')",
            name=op.f("ck_badges_source"),
        ),
        sa.CheckConstraint(
            "status <> 'active' OR category IS NOT NULL", name=op.f("ck_badges_active_category")
        ),
        sa.CheckConstraint(
            "status IN ('active', 'revoked', 'blocked')", name=op.f("ck_badges_status")
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["platform_accounts.id"],
            name=op.f("fk_badges_account_id_platform_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_badges")),
        sa.UniqueConstraint("account_id", name=op.f("uq_badges_account_id")),
        sa.UniqueConstraint("public_id", name=op.f("uq_badges_public_id")),
    )
    op.create_index("ix_badges_status_category", "badges", ["status", "category"], unique=False)
    op.create_index("ix_badges_wikidata_qid", "badges", ["wikidata_qid"], unique=False)
    op.create_table(
        "badge_affiliations",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "public_id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("member_account_id", sa.BigInteger(), nullable=False),
        sa.Column("org_badge_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("wikidata_property", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("validated_by", sa.Text(), nullable=True),
        sa.Column("validated_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("ended_by", sa.Text(), nullable=True),
        sa.Column("ended_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("end_reason", sa.Text(), nullable=True),
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
            "NOT is_primary OR status = 'active'", name=op.f("ck_badge_affiliations_primary_active")
        ),
        sa.CheckConstraint(
            "source IN ('manual', 'organization', 'wikidata', 'request')",
            name=op.f("ck_badge_affiliations_source"),
        ),
        sa.CheckConstraint(
            "status <> 'active' OR expires_at IS NOT NULL",
            name=op.f("ck_badge_affiliations_active_expiry"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'rejected', 'revoked', 'expired')",
            name=op.f("ck_badge_affiliations_status"),
        ),
        sa.ForeignKeyConstraint(
            ["member_account_id"],
            ["platform_accounts.id"],
            name=op.f("fk_badge_affiliations_member_account_id_platform_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_badge_id"],
            ["badges.id"],
            name=op.f("fk_badge_affiliations_org_badge_id_badges"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_badge_affiliations")),
        sa.UniqueConstraint(
            "member_account_id",
            "org_badge_id",
            name=op.f("uq_badge_affiliations_member_account_id_org_badge_id"),
        ),
        sa.UniqueConstraint("public_id", name=op.f("uq_badge_affiliations_public_id")),
    )
    op.create_index(
        "ix_badge_affiliations_expires_at",
        "badge_affiliations",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_badge_affiliations_org_status",
        "badge_affiliations",
        ["org_badge_id", "status"],
        unique=False,
    )
    op.create_index(
        "uq_badge_affiliations_primary",
        "badge_affiliations",
        ["member_account_id"],
        unique=True,
        postgresql_where=sa.text("is_primary"),
    )
    op.create_table(
        "badge_org_keys",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "public_id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("org_badge_id", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["org_badge_id"],
            ["badges.id"],
            name=op.f("fk_badge_org_keys_org_badge_id_badges"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_badge_org_keys")),
        sa.UniqueConstraint("public_id", name=op.f("uq_badge_org_keys_public_id")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_badge_org_keys_token_hash")),
    )
    op.create_index(
        op.f("ix_badge_org_keys_org_badge_id"), "badge_org_keys", ["org_badge_id"], unique=False
    )
    op.create_table(
        "badge_requests",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "public_id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("requested_category", sa.Text(), nullable=True),
        sa.Column("org_badge_id", sa.BigInteger(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("justification", sa.Text(), nullable=True),
        sa.Column("contact", sa.Text(), nullable=True),
        sa.Column(
            "evidence_urls",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("submitted_by", sa.Text(), nullable=False),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decided_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind <> 'affiliation' OR org_badge_id IS NOT NULL",
            name=op.f("ck_badge_requests_affiliation_org"),
        ),
        sa.CheckConstraint("kind IN ('badge', 'affiliation')", name=op.f("ck_badge_requests_kind")),
        sa.CheckConstraint(
            "requested_category IS NULL OR requested_category IN ('institutional', 'organization', 'media', 'expert', 'personality')",
            name=op.f("ck_badge_requests_requested_category"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')", name=op.f("ck_badge_requests_status")
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["platform_accounts.id"],
            name=op.f("fk_badge_requests_account_id_platform_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_badge_id"],
            ["badges.id"],
            name=op.f("fk_badge_requests_org_badge_id_badges"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_badge_requests")),
        sa.UniqueConstraint("public_id", name=op.f("uq_badge_requests_public_id")),
    )
    op.create_index(
        "ix_badge_requests_status_created", "badge_requests", ["status", "created_at"], unique=False
    )
    op.create_index(
        "uq_badge_requests_pending",
        "badge_requests",
        ["kind", "account_id", sa.literal_column("coalesce(org_badge_id, 0)")],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "badge_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("badge_id", sa.BigInteger(), nullable=True),
        sa.Column("affiliation_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "changes",
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
            "action IN ('granted', 'updated', 'revoked', 'blocked', 'unblocked', 'affiliation_created', 'affiliation_validated', 'affiliation_reconfirmed', 'affiliation_rejected', 'affiliation_revoked', 'affiliation_expired', 'affiliation_primary', 'org_key_created', 'org_key_revoked')",
            name=op.f("ck_badge_events_action"),
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["platform_accounts.id"],
            name=op.f("fk_badge_events_account_id_platform_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["affiliation_id"],
            ["badge_affiliations.id"],
            name=op.f("fk_badge_events_affiliation_id_badge_affiliations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["badge_id"],
            ["badges.id"],
            name=op.f("fk_badge_events_badge_id_badges"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_badge_events")),
    )
    op.create_index(
        op.f("ix_badge_events_account_id"), "badge_events", ["account_id"], unique=False
    )
    op.create_index(
        op.f("ix_badge_events_affiliation_id"), "badge_events", ["affiliation_id"], unique=False
    )
    op.create_index(op.f("ix_badge_events_badge_id"), "badge_events", ["badge_id"], unique=False)


def downgrade() -> None:
    for table in _TABLES:
        op.drop_table(table)
