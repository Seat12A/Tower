"""Source standing: ``unrated`` tier, standing and tier basis, domain identities.

Expand only, compatible with the code already deployed:

- ``sources.tier`` accepts ``unrated`` (unknown domains were stored as ``tertiary``); the
  CHECK is replaced ``NOT VALID`` then validated, without a long lock. The deployed code
  never writes ``unrated``;
- nullable ``sources.standing``, ``sources.tier_basis`` and ``evidence_passages.standing``
  (ignored by the deployed code; legacy rows stay NULL);
- new table ``domain_identities`` (filled by the ``atlas.identity_refresh`` job).

Revision : 0013
Parents : 0012
Created : 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STANDINGS = (
    "'official', 'academic', 'journal', 'press', 'fact_checker', 'reference', "
    "'organization', 'user_generated', 'commercial', 'first_party', 'unclear'"
)
_BASES = "'registry', 'adapter', 'identity', 'suffix', 'default'"
_OLD_TIERS = "tier IN ('primary', 'secondary', 'tertiary', 'avoid')"
_NEW_TIERS = "tier IN ('primary', 'secondary', 'tertiary', 'avoid', 'unrated')"


def _replace_check(table: str, name: str, condition: str) -> None:
    op.drop_constraint(op.f(name), table, type_="check")
    op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({condition}) NOT VALID")
    op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}")


def upgrade() -> None:
    _replace_check("sources", "ck_sources_tier", _NEW_TIERS)
    op.add_column("sources", sa.Column("standing", sa.Text(), nullable=True))
    op.add_column("sources", sa.Column("tier_basis", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_sources_standing"), "sources", f"standing IS NULL OR standing IN ({_STANDINGS})"
    )
    op.create_check_constraint(
        op.f("ck_sources_tier_basis"),
        "sources",
        f"tier_basis IS NULL OR tier_basis IN ({_BASES})",
    )
    op.add_column("evidence_passages", sa.Column("standing", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_evidence_passages_standing"),
        "evidence_passages",
        f"standing IS NULL OR standing IN ({_STANDINGS})",
    )
    op.create_table(
        "domain_identities",
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("qid", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("country", sa.Text(), nullable=True),
        sa.Column("origin", sa.Text(), server_default=sa.text("'wikidata'"), nullable=False),
        sa.Column(
            "refreshed_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('official', 'academic', 'journal', 'press')",
            name=op.f("ck_domain_identities_kind"),
        ),
        sa.CheckConstraint("origin IN ('wikidata')", name=op.f("ck_domain_identities_origin")),
        sa.PrimaryKeyConstraint("domain", name=op.f("pk_domain_identities")),
    )
    op.create_index(
        "ix_domain_identities_refreshed_at", "domain_identities", ["refreshed_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_domain_identities_refreshed_at", table_name="domain_identities")
    op.drop_table("domain_identities")
    op.drop_constraint(op.f("ck_evidence_passages_standing"), "evidence_passages", type_="check")
    op.drop_column("evidence_passages", "standing")
    op.drop_constraint(op.f("ck_sources_tier_basis"), "sources", type_="check")
    op.drop_constraint(op.f("ck_sources_standing"), "sources", type_="check")
    op.drop_column("sources", "tier_basis")
    op.drop_column("sources", "standing")
    op.execute("UPDATE sources SET tier = 'tertiary' WHERE tier = 'unrated'")
    _replace_check("sources", "ck_sources_tier", _OLD_TIERS)
