"""Individual permissions of team members: ``staff_members.permissions``.

Expand only: a NOT NULL column with a constant default (added without rewriting the
table). The code already deployed ignores it and keeps resolving members from ``roles``;
the new code reads ``roles`` (presets) and ``permissions`` (explicit list) together.

Revision : 0010
Parents : 0009
Created : 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "staff_members",
        sa.Column(
            "permissions",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("staff_members", "permissions")
