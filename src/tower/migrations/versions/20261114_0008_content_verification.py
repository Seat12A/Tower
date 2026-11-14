"""Verification of extension contents: ``content_items.verification`` and
``checked_at``, with the indexes of the embed check.

Expand only: a NOT NULL column with a constant default (added without rewriting the
table), a nullable column, a CHECK on the new column, two partial indexes. The lot 5
code runs unchanged on this schema (its inserts take the default ``unverified``).

Backfill: contents observed through a service source (radar, feed, manual), in the
detailed observations or in the hourly counters, become ``platform``.

Revision : 0008
Parents : 0007
Created : 2026-11-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VALUES = "'unverified', 'platform', 'verified', 'mismatch', 'unavailable'"


def upgrade() -> None:
    op.add_column(
        "content_items",
        sa.Column(
            "verification", sa.Text(), server_default=sa.text("'unverified'"), nullable=False
        ),
    )
    op.add_column("content_items", sa.Column("checked_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        op.f("ck_content_items_verification"), "content_items", f"verification IN ({_VALUES})"
    )
    op.execute(
        """
        UPDATE content_items c SET verification = 'platform'
        WHERE EXISTS (
            SELECT 1 FROM observations o
            WHERE o.content_id = c.id AND o.source IN ('radar', 'feed', 'manual')
        ) OR EXISTS (
            SELECT 1 FROM observation_counters k
            WHERE k.content_id = c.id AND k.source IN ('radar', 'feed', 'manual')
        )
        """
    )
    op.create_index(
        "ix_content_items_to_check",
        "content_items",
        ["first_seen_at"],
        postgresql_where=sa.text("verification = 'unverified' AND checked_at IS NULL"),
    )
    op.create_index(
        "ix_content_items_checked_at",
        "content_items",
        ["checked_at"],
        postgresql_where=sa.text("checked_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_content_items_checked_at", table_name="content_items")
    op.drop_index("ix_content_items_to_check", table_name="content_items")
    op.drop_constraint(op.f("ck_content_items_verification"), "content_items", type_="check")
    op.drop_column("content_items", "checked_at")
    op.drop_column("content_items", "verification")
