"""Organisation icons stored by Tower (``badge_logos``).

Expand only: a new table, unused by the deployed code.

Revision : 0018
Parents : 0017
Created : 2026-11-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "badge_logos",
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("width", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("height", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "content_type IN ('image/png', 'image/x-icon', 'image/jpeg', 'image/gif', "
            "'image/webp')",
            name=op.f("ck_badge_logos_content_type"),
        ),
        sa.CheckConstraint("octet_length(data) <= 200000", name=op.f("ck_badge_logos_data_size")),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_badge_logos_sha256")),
        sa.PrimaryKeyConstraint("sha256", name=op.f("pk_badge_logos")),
    )


def downgrade() -> None:
    op.drop_table("badge_logos")
