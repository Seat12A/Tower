"""Complete Atlas (lot 6): LEGI mirror, country registries, news index, editable
reliability registry, synchronization state and confidence calibration models.

Expand only: new tables; nullable columns or columns with a constant default on
``source_registry`` (added without rewriting the table) and CHECKs on those new columns;
``reference_facts.origin`` accepts one more value (``statistics``). The lot 5 code runs
unchanged on this schema (it never writes the new columns nor the new origin).

Revision : 0009
Parents : 0008
Created : 2026-11-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "atlas_sync_state",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column(
            "state",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_atlas_sync_state")),
    )
    op.create_table(
        "confidence_models",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("coefficients", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("reviews", sa.Integer(), nullable=False),
        sa.Column("refusals", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("activated_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("activated_by", sa.Text(), nullable=True),
        sa.CheckConstraint("kind IN ('reduced', 'full')", name=op.f("ck_confidence_models_kind")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_confidence_models")),
    )
    op.create_index(
        "uq_confidence_models_active",
        "confidence_models",
        ["active"],
        unique=True,
        postgresql_where=sa.text("active"),
    )
    op.create_table(
        "election_calendar",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("country", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("first_round", sa.Date(), nullable=False),
        sa.Column("second_round", sa.Date(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "country ~ '^([A-Z]{2}(-[A-Z0-9]{1,3})?|EU|INT)$'",
            name=op.f("ck_election_calendar_country_format"),
        ),
        sa.CheckConstraint(
            "kind IN ('presidential', 'legislative', 'european', 'local', 'regional', 'referendum', 'other')",
            name=op.f("ck_election_calendar_kind"),
        ),
        sa.CheckConstraint(
            "second_round IS NULL OR second_round >= first_round",
            name=op.f("ck_election_calendar_rounds_order"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_election_calendar")),
        sa.UniqueConstraint("country", "kind", "first_round", name="uq_election_calendar_entry"),
    )
    op.create_index(
        "ix_election_calendar_first_round", "election_calendar", ["first_round"], unique=False
    )
    op.create_table(
        "jurisdiction_sources",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("country", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("adapter", sa.Text(), nullable=True),
        sa.Column("domain", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("tier", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "country ~ '^([A-Z]{2}(-[A-Z0-9]{1,3})?|EU|INT)$'",
            name=op.f("ck_jurisdiction_sources_country_format"),
        ),
        sa.CheckConstraint(
            "source_type IN ('statistics', 'law', 'electoral', 'central_bank', 'health', 'justice', 'news', 'other')",
            name=op.f("ck_jurisdiction_sources_source_type"),
        ),
        sa.CheckConstraint(
            "tier IN ('primary', 'secondary', 'tertiary', 'avoid')",
            name=op.f("ck_jurisdiction_sources_tier"),
        ),
        sa.CheckConstraint(
            "adapter IS NOT NULL OR domain IS NOT NULL",
            name=op.f("ck_jurisdiction_sources_adapter_or_domain"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jurisdiction_sources")),
        sa.UniqueConstraint("country", "source_type", "key", name="uq_jurisdiction_sources_key"),
    )
    op.create_table(
        "legal_articles",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("jurisdiction", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("article_id", sa.Text(), nullable=False),
        sa.Column("text_id", sa.Text(), nullable=False),
        sa.Column("text_title", sa.Text(), nullable=False),
        sa.Column("number", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("section_path", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("sha256", sa.LargeBinary(), nullable=False),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('french'::regconfig, coalesce(number, '') || ' ' || body)",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "synced_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_from IS NULL OR valid_from <= valid_to",
            name=op.f("ck_legal_articles_validity_order"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_legal_articles")),
        sa.UniqueConstraint("source", "article_id", name="uq_legal_articles_source_article"),
    )
    op.create_index(
        "ix_legal_articles_lookup",
        "legal_articles",
        ["jurisdiction", "text_id", "number", "valid_from"],
        unique=False,
    )
    op.create_index(
        "ix_legal_articles_tsv", "legal_articles", ["tsv"], unique=False, postgresql_using="gin"
    )
    op.create_table(
        "legal_portals",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("country", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("eli", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("access", sa.Text(), nullable=False),
        sa.Column("adapter", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "access IN ('bulk', 'api', 'web', 'unverified')", name=op.f("ck_legal_portals_access")
        ),
        sa.CheckConstraint(
            "country ~ '^([A-Z]{2}(-[A-Z0-9]{1,3})?|EU|INT)$'",
            name=op.f("ck_legal_portals_country_format"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_legal_portals")),
        sa.UniqueConstraint("country", "url", name="uq_legal_portals_country_url"),
    )
    op.create_table(
        "news_feeds",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("country", sa.Text(), nullable=False),
        sa.Column("lang", sa.Text(), nullable=False),
        sa.Column("publisher", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column("last_modified", sa.Text(), nullable=True),
        sa.Column("last_polled_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_status", sa.SmallInteger(), nullable=True),
        sa.Column("failures", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "country ~ '^([A-Z]{2}(-[A-Z0-9]{1,3})?|EU|INT)$'",
            name=op.f("ck_news_feeds_country_format"),
        ),
        sa.CheckConstraint(
            "kind IN ('public_service', 'national', 'regional', 'agency', 'international')",
            name=op.f("ck_news_feeds_kind"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_news_feeds")),
        sa.UniqueConstraint("url", name=op.f("uq_news_feeds_url")),
    )
    op.create_table(
        "news_index",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("feed_id", sa.BigInteger(), nullable=True),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("publisher", sa.Text(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("country", sa.Text(), nullable=True),
        sa.Column("lang", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("published_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "fetched_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("simhash", sa.BigInteger(), nullable=False),
        sa.Column("simhash_b0", sa.Integer(), nullable=False),
        sa.Column("simhash_b1", sa.Integer(), nullable=False),
        sa.Column("simhash_b2", sa.Integer(), nullable=False),
        sa.Column("simhash_b3", sa.Integer(), nullable=False),
        sa.Column("cluster_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('simple'::regconfig, title || ' ' || coalesce(summary, ''))",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.CheckConstraint("origin IN ('rss', 'gdelt')", name=op.f("ck_news_index_origin")),
        sa.ForeignKeyConstraint(
            ["feed_id"],
            ["news_feeds.id"],
            name=op.f("fk_news_index_feed_id_news_feeds"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_news_index")),
        sa.UniqueConstraint("canonical_url", name=op.f("uq_news_index_canonical_url")),
    )
    op.create_index("ix_news_index_b0", "news_index", ["simhash_b0", "published_at"], unique=False)
    op.create_index("ix_news_index_b1", "news_index", ["simhash_b1", "published_at"], unique=False)
    op.create_index("ix_news_index_b2", "news_index", ["simhash_b2", "published_at"], unique=False)
    op.create_index("ix_news_index_b3", "news_index", ["simhash_b3", "published_at"], unique=False)
    op.create_index(op.f("ix_news_index_cluster_id"), "news_index", ["cluster_id"], unique=False)
    op.create_index(op.f("ix_news_index_feed_id"), "news_index", ["feed_id"], unique=False)
    op.create_index("ix_news_index_published_at", "news_index", ["published_at"], unique=False)
    op.create_index(
        "ix_news_index_tsv", "news_index", ["tsv"], unique=False, postgresql_using="gin"
    )
    op.create_table(
        "source_registry_versions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("registry_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("change", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("changed_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["registry_id"],
            ["source_registry.id"],
            name=op.f("fk_source_registry_versions_registry_id_source_registry"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_registry_versions")),
        sa.UniqueConstraint("registry_id", "version", name="uq_source_registry_versions"),
    )
    op.create_index(
        op.f("ix_source_registry_versions_registry_id"),
        "source_registry_versions",
        ["registry_id"],
        unique=False,
    )
    op.add_column(
        "source_registry",
        sa.Column(
            "aliases",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
    )
    op.add_column("source_registry", sa.Column("qid", sa.Text(), nullable=True))
    op.add_column("source_registry", sa.Column("controlled_by", sa.Text(), nullable=True))
    op.add_column("source_registry", sa.Column("controlled_by_qid", sa.Text(), nullable=True))
    op.add_column(
        "source_registry",
        sa.Column(
            "controlled_by_aliases",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
    )
    op.add_column("source_registry", sa.Column("control_origin", sa.Text(), nullable=True))
    op.add_column(
        "source_registry",
        sa.Column(
            "statutory_independence", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.add_column("source_registry", sa.Column("independence_basis", sa.Text(), nullable=True))
    op.add_column(
        "source_registry",
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.add_column("source_registry", sa.Column("updated_by", sa.Text(), nullable=True))
    op.add_column(
        "source_registry",
        sa.Column("reviewed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_source_registry_control_origin"),
        "source_registry",
        "control_origin IS NULL OR control_origin IN ('wikidata', 'team', 'official')",
    )
    op.create_check_constraint(
        op.f("ck_source_registry_independence_basis"),
        "source_registry",
        "independence_basis IS NULL OR independence_basis IN "
        "('imf_sdds', 'imf_sdds_plus', 'ess_peer_review', 'statute')",
    )
    op.create_check_constraint(
        op.f("ck_source_registry_independence_needs_basis"),
        "source_registry",
        "NOT statutory_independence OR independence_basis IS NOT NULL",
    )
    op.drop_constraint(op.f("ck_reference_facts_origin"), "reference_facts", type_="check")
    op.create_check_constraint(
        op.f("ck_reference_facts_origin"),
        "reference_facts",
        "origin IN ('investigation', 'insee', 'manual', 'statistics')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_reference_facts_origin"), "reference_facts", type_="check")
    op.execute("DELETE FROM reference_facts WHERE origin = 'statistics'")
    op.create_check_constraint(
        op.f("ck_reference_facts_origin"),
        "reference_facts",
        "origin IN ('investigation', 'insee', 'manual')",
    )
    for name in ("control_origin", "independence_basis", "independence_needs_basis"):
        op.drop_constraint(op.f(f"ck_source_registry_{name}"), "source_registry", type_="check")
    op.drop_column("source_registry", "reviewed_at")
    op.drop_column("source_registry", "updated_by")
    op.drop_column("source_registry", "active")
    op.drop_column("source_registry", "independence_basis")
    op.drop_column("source_registry", "statutory_independence")
    op.drop_column("source_registry", "control_origin")
    op.drop_column("source_registry", "controlled_by_aliases")
    op.drop_column("source_registry", "controlled_by_qid")
    op.drop_column("source_registry", "controlled_by")
    op.drop_column("source_registry", "qid")
    op.drop_column("source_registry", "aliases")
    op.drop_index(
        op.f("ix_source_registry_versions_registry_id"), table_name="source_registry_versions"
    )
    op.drop_table("source_registry_versions")
    op.drop_index("ix_news_index_tsv", table_name="news_index", postgresql_using="gin")
    op.drop_index("ix_news_index_published_at", table_name="news_index")
    op.drop_index(op.f("ix_news_index_feed_id"), table_name="news_index")
    op.drop_index(op.f("ix_news_index_cluster_id"), table_name="news_index")
    op.drop_index("ix_news_index_b3", table_name="news_index")
    op.drop_index("ix_news_index_b2", table_name="news_index")
    op.drop_index("ix_news_index_b1", table_name="news_index")
    op.drop_index("ix_news_index_b0", table_name="news_index")
    op.drop_table("news_index")
    op.drop_table("news_feeds")
    op.drop_table("legal_portals")
    op.drop_index("ix_legal_articles_tsv", table_name="legal_articles", postgresql_using="gin")
    op.drop_index("ix_legal_articles_lookup", table_name="legal_articles")
    op.drop_table("legal_articles")
    op.drop_table("jurisdiction_sources")
    op.drop_index("ix_election_calendar_first_round", table_name="election_calendar")
    op.drop_table("election_calendar")
    op.drop_index(
        "uq_confidence_models_active",
        table_name="confidence_models",
        postgresql_where=sa.text("active"),
    )
    op.drop_table("confidence_models")
    op.drop_table("atlas_sync_state")
