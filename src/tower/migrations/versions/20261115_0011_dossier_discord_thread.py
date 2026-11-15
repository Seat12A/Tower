"""Discord forum post of a dossier: ``dossiers.discord_thread_id``.

Expand only: a nullable column, added without rewriting the table. The code already
deployed ignores it; the new intercom records the forum post (thread) it created for
each dossier so it can update it after a restart.

Revision : 0011
Parents : 0010
Created : 2026-11-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("dossiers", sa.Column("discord_thread_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("dossiers", "discord_thread_id")
