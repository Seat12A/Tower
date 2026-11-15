"""Rename dossiers to cases (tables, columns, indexes, constraints).

Expand step of an expand/contract rename. The pre-deploy runs this before the new code
is live, so the code already deployed must keep working: each renamed table gets a
**compatibility view** under its old name (simple views are automatically updatable in
PostgreSQL: SELECT, INSERT with the base table defaults, UPDATE, DELETE and RETURNING
keep working; renamed columns are exposed under their old names). The snapshot reason
``dossier_opened`` stays accepted next to ``case_opened``.

Contract (a later migration, once no deployed code uses the old names): drop the views,
rewrite stored ``dossier_opened`` reasons and restrict the CHECK to ``case_opened``.

Revision : 0012
Parents : 0011
Created : 2026-11-15
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (("dossiers", "cases"), ("dossier_events", "case_events"))
_TABLES_RENAMED = (*_TABLES, ("dossier_claim_groups", "case_claim_groups"))
_COLUMNS = (
    ("case_events", "dossier_id", "case_id"),
    ("case_claim_groups", "dossier_id", "case_id"),
    ("reviews", "dossier_id", "case_id"),
)
_INDEXES = (
    ("ix_dossiers_status_kind_created", "ix_cases_status_kind_created"),
    ("ix_dossiers_x_post_id", "ix_cases_x_post_id"),
    ("ix_dossiers_content_id", "ix_cases_content_id"),
    ("ix_dossier_events_dossier_id", "ix_case_events_case_id"),
    ("ix_dossier_claim_groups_claim_group_id", "ix_case_claim_groups_claim_group_id"),
    ("ix_reviews_dossier_id", "ix_reviews_case_id"),
)
_CONSTRAINTS = (
    ("cases", "pk_dossiers", "pk_cases"),
    ("cases", "uq_dossiers_public_id", "uq_cases_public_id"),
    ("cases", "ck_dossiers_kind", "ck_cases_kind"),
    ("cases", "ck_dossiers_status", "ck_cases_status"),
    ("cases", "fk_dossiers_content_id_content_items", "fk_cases_content_id_content_items"),
    ("case_events", "pk_dossier_events", "pk_case_events"),
    ("case_events", "fk_dossier_events_dossier_id_dossiers", "fk_case_events_case_id_cases"),
    ("case_claim_groups", "pk_dossier_claim_groups", "pk_case_claim_groups"),
    (
        "case_claim_groups",
        "fk_dossier_claim_groups_dossier_id_dossiers",
        "fk_case_claim_groups_case_id_cases",
    ),
    (
        "case_claim_groups",
        "fk_dossier_claim_groups_claim_group_id_claim_groups",
        "fk_case_claim_groups_claim_group_id_claim_groups",
    ),
    ("reviews", "fk_reviews_dossier_id_dossiers", "fk_reviews_case_id_cases"),
)
_SEQUENCES = (("dossiers_id_seq", "cases_id_seq"), ("dossier_events_id_seq", "case_events_id_seq"))
# Old view name -> SELECT exposing the old column names.
_VIEWS = (
    ("dossiers", "SELECT * FROM cases"),
    (
        "dossier_events",
        "SELECT id, case_id AS dossier_id, from_status, to_status, actor, reason, data, "
        "created_at FROM case_events",
    ),
    ("dossier_claim_groups", "SELECT case_id AS dossier_id, claim_group_id FROM case_claim_groups"),
)
_REASONS_BOTH = "reason IN ('case_opened', 'dossier_opened', 'recheck', 'manual')"
_REASONS_OLD = "reason IN ('dossier_opened', 'recheck', 'manual')"


def upgrade() -> None:
    for old, new in _TABLES_RENAMED:
        op.rename_table(old, new)
    for table, old, new in _COLUMNS:
        op.alter_column(table, old, new_column_name=new)
    for old, new in _INDEXES:
        op.execute(f"ALTER INDEX IF EXISTS {old} RENAME TO {new}")
    for table, old, new in _CONSTRAINTS:
        op.execute(f"ALTER TABLE {table} RENAME CONSTRAINT {old} TO {new}")
    for old, new in _SEQUENCES:
        op.execute(f"ALTER SEQUENCE IF EXISTS {old} RENAME TO {new}")
    for name, select in _VIEWS:
        op.execute(f"CREATE VIEW {name} AS {select}")
    op.drop_constraint(op.f("ck_content_snapshots_reason"), "content_snapshots", type_="check")
    op.create_check_constraint(
        op.f("ck_content_snapshots_reason"), "content_snapshots", _REASONS_BOTH
    )


def downgrade() -> None:
    op.execute(
        "UPDATE content_snapshots SET reason = 'dossier_opened' WHERE reason = 'case_opened'"
    )
    op.drop_constraint(op.f("ck_content_snapshots_reason"), "content_snapshots", type_="check")
    op.create_check_constraint(
        op.f("ck_content_snapshots_reason"), "content_snapshots", _REASONS_OLD
    )
    for name, _select in reversed(_VIEWS):
        op.execute(f"DROP VIEW {name}")
    for old, new in _SEQUENCES:
        op.execute(f"ALTER SEQUENCE IF EXISTS {new} RENAME TO {old}")
    for table, old, new in _CONSTRAINTS:
        op.execute(f"ALTER TABLE {table} RENAME CONSTRAINT {new} TO {old}")
    for old, new in _INDEXES:
        op.execute(f"ALTER INDEX IF EXISTS {new} RENAME TO {old}")
    for table, old, new in _COLUMNS:
        op.alter_column(table, new, new_column_name=old)
    for old, new in reversed(_TABLES_RENAMED):
        op.rename_table(new, old)
