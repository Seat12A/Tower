"""Badges: field of expertise of expert badges, "verified" category (expand).

Expand only: new nullable column ``badges.expertise``; the category checks of
``badges`` and ``badge_requests`` also accept ``verified`` (former ``personality``,
still accepted). The deployed code ignores the column and never writes ``verified``.
Existing ``personality`` rows are rewritten by a later contract migration, once no
deployed code reads them as ``personality`` any more.

Revision : 0015
Parents : 0014
Created : 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "'institutional', 'organization', 'media', 'expert', 'personality'"
_NEW = "'institutional', 'organization', 'media', 'expert', 'verified', 'personality'"


def _replace_checks(values: str) -> None:
    op.drop_constraint(op.f("ck_badges_category"), "badges", type_="check")
    op.create_check_constraint(
        op.f("ck_badges_category"), "badges", f"category IS NULL OR category IN ({values})"
    )
    op.drop_constraint(
        op.f("ck_badge_requests_requested_category"), "badge_requests", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_badge_requests_requested_category"),
        "badge_requests",
        f"requested_category IS NULL OR requested_category IN ({values})",
    )


def upgrade() -> None:
    op.add_column("badges", sa.Column("expertise", sa.Text(), nullable=True))
    _replace_checks(_NEW)


def downgrade() -> None:
    op.execute("UPDATE badges SET category = 'personality' WHERE category = 'verified'")
    op.execute(
        "UPDATE badge_requests SET requested_category = 'personality'"
        " WHERE requested_category = 'verified'"
    )
    _replace_checks(_OLD)
    op.drop_column("badges", "expertise")
