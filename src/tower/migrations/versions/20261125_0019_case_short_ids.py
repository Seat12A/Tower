"""Case short ids (``cases.short_id``): the identifier shown everywhere.

The first 8 hex digits of ``public_id``, one more digit per collision, set once by a
``BEFORE INSERT`` trigger and never changed afterwards (a published link stays valid).
Expand only: the trigger fills the column for the code already deployed too, which
ignores it.

Revision : 0019
Parents : 0018
Created : 2026-11-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FUNCTION = """
CREATE FUNCTION cases_short_id() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    digits text;
    n int := 8;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        NEW.short_id := OLD.short_id;
        RETURN NEW;
    END IF;
    digits := replace(NEW.public_id::text, '-', '');
    LOOP
        EXIT WHEN n >= 32
            OR NOT EXISTS (SELECT 1 FROM cases WHERE short_id = left(digits, n));
        n := n + 1;
    END LOOP;
    NEW.short_id := left(digits, n);
    RETURN NEW;
END
$$
"""

_BACKFILL = """
DO $$
DECLARE
    row record;
    digits text;
    n int;
BEGIN
    FOR row IN SELECT id, public_id FROM cases WHERE short_id IS NULL ORDER BY id LOOP
        digits := replace(row.public_id::text, '-', '');
        n := 8;
        LOOP
            EXIT WHEN n >= 32
                OR NOT EXISTS (SELECT 1 FROM cases WHERE short_id = left(digits, n));
            n := n + 1;
        END LOOP;
        UPDATE cases SET short_id = left(digits, n) WHERE id = row.id;
    END LOOP;
END
$$
"""


def upgrade() -> None:
    op.add_column("cases", sa.Column("short_id", sa.Text(), nullable=True))
    op.execute(_BACKFILL)
    op.execute(_FUNCTION)
    op.execute(
        "CREATE TRIGGER cases_short_id BEFORE INSERT OR UPDATE OF short_id ON cases "
        "FOR EACH ROW EXECUTE FUNCTION cases_short_id()"
    )
    op.alter_column("cases", "short_id", nullable=False)
    op.create_unique_constraint(op.f("uq_cases_short_id"), "cases", ["short_id"])
    op.create_check_constraint(
        op.f("ck_cases_short_id_hex"), "cases", "short_id ~ '^[0-9a-f]{8,32}$'"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_cases_short_id_hex"), "cases", type_="check")
    op.drop_constraint(op.f("uq_cases_short_id"), "cases", type_="unique")
    op.execute("DROP TRIGGER cases_short_id ON cases")
    op.execute("DROP FUNCTION cases_short_id()")
    op.drop_column("cases", "short_id")
