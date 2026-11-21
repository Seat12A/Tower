"""Index of content items by verdict index bucket (first 2 bytes of sha256(external_id)).

Serves ``GET /v1/verdict-index/{platform}/{bucket}``: the extension shows verdicts on
every post without telling gate which post it shows.

Expand only: one index, built ``CONCURRENTLY`` (content_items can be large; writes keep
going during the build). The deployed code ignores it.

Revision : 0016
Parents : 0015
Created : 2026-11-21
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # A failed concurrent build leaves an invalid index: drop it before retrying.
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_content_items_verdict_bucket")
        op.execute(
            "CREATE INDEX CONCURRENTLY ix_content_items_verdict_bucket ON content_items "
            "(platform, substring(sha256(external_id::bytea) FROM 1 FOR 2))"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_content_items_verdict_bucket")
