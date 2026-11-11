"""Radar scheduling, budget reservations, watched accounts pending resolution.

Expand only: new table, new nullable columns, ``platform_accounts.external_id`` becomes
nullable (accounts added by handle, resolved by the radar). SSE client cursors reuse
``event_cursors`` (no table).

Revision : 0004
Parents : 0003
Created : 2026-11-11 18:56:30.523849
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "budget_reservations",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("counter_id", sa.BigInteger(), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("ref", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(kind = 'budget') = (counter_id IS NOT NULL)",
            name=op.f("ck_budget_reservations_counter_for_budget"),
        ),
        sa.CheckConstraint("kind IN ('budget', 'free')", name=op.f("ck_budget_reservations_kind")),
        sa.CheckConstraint("amount > 0", name=op.f("ck_budget_reservations_amount")),
        sa.ForeignKeyConstraint(
            ["counter_id"],
            ["budget_counters.id"],
            name=op.f("fk_budget_reservations_counter_id_budget_counters"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_budget_reservations")),
    )
    op.create_index(
        op.f("ix_budget_reservations_counter_id"),
        "budget_reservations",
        ["counter_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_budget_reservations_expires_at"),
        "budget_reservations",
        ["expires_at"],
        unique=False,
    )
    op.alter_column("platform_accounts", "external_id", existing_type=sa.TEXT(), nullable=True)
    op.create_check_constraint(
        op.f("ck_platform_accounts_identified"),
        "platform_accounts",
        "external_id IS NOT NULL OR handle IS NOT NULL",
    )
    op.create_index(
        "uq_platform_accounts_pending_handle",
        "platform_accounts",
        ["platform", sa.literal_column("lower(handle)")],
        unique=True,
        postgresql_where=sa.text("external_id IS NULL"),
    )
    op.add_column(
        "watched_accounts",
        sa.Column("next_poll_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "watched_accounts",
        sa.Column("last_polled_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column("watched_accounts", sa.Column("poll_interval_s", sa.Integer(), nullable=True))
    op.add_column(
        "watched_accounts",
        sa.Column("last_post_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "watched_accounts",
        sa.Column(
            "consecutive_empty", sa.SmallInteger(), server_default=sa.text("0"), nullable=False
        ),
    )
    op.create_index(
        "ix_watched_accounts_next_poll_at",
        "watched_accounts",
        ["next_poll_at"],
        unique=False,
        postgresql_where=sa.text("active"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_watched_accounts_next_poll_at",
        table_name="watched_accounts",
        postgresql_where=sa.text("active"),
    )
    op.drop_column("watched_accounts", "consecutive_empty")
    op.drop_column("watched_accounts", "last_post_at")
    op.drop_column("watched_accounts", "poll_interval_s")
    op.drop_column("watched_accounts", "last_polled_at")
    op.drop_column("watched_accounts", "next_poll_at")
    op.drop_index(
        "uq_platform_accounts_pending_handle",
        table_name="platform_accounts",
        postgresql_where=sa.text("external_id IS NULL"),
    )
    op.drop_constraint(op.f("ck_platform_accounts_identified"), "platform_accounts", type_="check")
    # Accounts still pending resolution cannot exist without an external id.
    op.execute(
        "DELETE FROM watched_account_events WHERE watched_account_id IN (SELECT w.id FROM"
        " watched_accounts w JOIN platform_accounts a ON a.id = w.account_id"
        " WHERE a.external_id IS NULL)"
    )
    op.execute(
        "DELETE FROM watched_accounts WHERE account_id IN"
        " (SELECT id FROM platform_accounts WHERE external_id IS NULL)"
    )
    op.execute("DELETE FROM platform_accounts WHERE external_id IS NULL")
    op.alter_column("platform_accounts", "external_id", existing_type=sa.TEXT(), nullable=False)
    op.drop_index(op.f("ix_budget_reservations_expires_at"), table_name="budget_reservations")
    op.drop_index(op.f("ix_budget_reservations_counter_id"), table_name="budget_reservations")
    op.drop_table("budget_reservations")
