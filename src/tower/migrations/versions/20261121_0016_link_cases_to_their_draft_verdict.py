"""Link each case to the draft verdict it was opened for (``cases.verdict_id``).

A claim group can hold several cases over time (one per draft). Draft actions (publish,
review, forum card) used to take the group's latest case, which could be another case.

Expand only: a nullable column, a unique constraint and a foreign key. The deployed code
ignores the column; cases it opens before the new code runs stay unlinked and are
resolved by the new code only when unambiguous.

Backfill: each fact-check case gets the drafted verdict of its claim group whose
``drafted_at`` is closest to the case's ``created_at`` (both are written in the same
transaction), within 5 minutes, one case per verdict. Anything ambiguous stays unlinked.

Revision : 0016
Parents : 0015
Created : 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BACKFILL = """
WITH pick AS (
    SELECT DISTINCT ON (c.id)
        c.id AS case_id,
        v.id AS verdict_id,
        abs(extract(epoch FROM v.drafted_at - c.created_at)) AS gap
    FROM cases c
    JOIN case_claim_groups ccg ON ccg.case_id = c.id
    JOIN verdicts v ON v.claim_group_id = ccg.claim_group_id AND v.drafted_at IS NOT NULL
    WHERE c.verdict_id IS NULL
      AND c.kind = 'fact_check'
      AND abs(extract(epoch FROM v.drafted_at - c.created_at)) <= 300
    ORDER BY c.id, gap, v.id
),
uniq AS (
    SELECT DISTINCT ON (verdict_id) case_id, verdict_id
    FROM pick
    ORDER BY verdict_id, gap, case_id
)
UPDATE cases c
SET verdict_id = u.verdict_id
FROM uniq u
WHERE c.id = u.case_id
  AND NOT EXISTS (SELECT 1 FROM cases o WHERE o.verdict_id = u.verdict_id)
"""


def upgrade() -> None:
    op.add_column("cases", sa.Column("verdict_id", sa.BigInteger(), nullable=True))
    op.create_unique_constraint(op.f("uq_cases_verdict_id"), "cases", ["verdict_id"])
    op.create_foreign_key(
        op.f("fk_cases_verdict_id_verdicts"),
        "cases",
        "verdicts",
        ["verdict_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(_BACKFILL)


def downgrade() -> None:
    op.drop_constraint(op.f("fk_cases_verdict_id_verdicts"), "cases", type_="foreignkey")
    op.drop_constraint(op.f("uq_cases_verdict_id"), "cases", type_="unique")
    op.drop_column("cases", "verdict_id")
