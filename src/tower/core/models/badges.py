"""Verification badges, affiliations and their history. See docs/badges.md."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from tower.core.db.base import Base
from tower.core.domain.badge_icons import ICON_TYPES
from tower.core.domain.badges import (
    LEGACY_CATEGORIES,
    AffiliationSource,
    AffiliationStatus,
    BadgeAction,
    BadgeCategory,
    BadgeSource,
    BadgeStatus,
    ReportReason,
    ReportStatus,
    RequestKind,
    RequestStatus,
)
from tower.core.models._helpers import check_in


def _public_id() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), unique=True, server_default=sa_text("gen_random_uuid()")
    )


_CATEGORY_SQL = ", ".join(f"'{v}'" for v in [*BadgeCategory, *LEGACY_CATEGORIES])
"""Allowed stored categories: current values, plus legacy ones until the contract
migration rewrites the old rows (docs/badges.md)."""


class Badge(Base):
    """Badge state of one platform account (one row per account, any status)."""

    __tablename__ = "badges"
    __table_args__ = (
        check_in("status", BadgeStatus, "status"),
        CheckConstraint(
            "category IS NULL OR category IN (" + _CATEGORY_SQL + ")",
            name="category",
        ),
        CheckConstraint("status <> 'active' OR category IS NOT NULL", name="active_category"),
        CheckConstraint(
            "source IS NULL OR source IN (" + ", ".join(f"'{s.value}'" for s in BadgeSource) + ")",
            name="source",
        ),
        Index("ix_badges_status_category", "status", "category"),
        Index("ix_badges_wikidata_qid", "wikidata_qid"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[uuid.UUID] = _public_id()
    account_id: Mapped[int] = mapped_column(
        ForeignKey("platform_accounts.id", ondelete="CASCADE"), unique=True
    )
    status: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)
    """Source of the current (or last) grant."""
    label: Mapped[str | None] = mapped_column(Text)
    """Public display name (shown on affiliates for an organisation)."""
    logo_url: Mapped[str | None] = mapped_column(Text)
    """Organisation logo, shown next to the handle of its affiliated accounts."""
    expertise: Mapped[str | None] = mapped_column(Text)
    """Field of an expert badge (French free text); None for other categories."""
    wikidata_qid: Mapped[str | None] = mapped_column(Text)
    wikidata_seen_at: Mapped[datetime | None]
    note: Mapped[str | None] = mapped_column(Text)
    """Internal team note, never exposed publicly."""
    granted_by: Mapped[str | None] = mapped_column(Text)
    granted_at: Mapped[datetime | None]
    ended_by: Mapped[str | None] = mapped_column(Text)
    """Actor of the last revocation or block."""
    ended_at: Mapped[datetime | None]
    end_reason: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, server_default=sa_text("1"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class BadgeAffiliation(Base):
    """Link between an individual account and an organisation badge."""

    __tablename__ = "badge_affiliations"
    __table_args__ = (
        UniqueConstraint("member_account_id", "org_badge_id"),
        check_in("status", AffiliationStatus, "status"),
        check_in("source", AffiliationSource, "source"),
        CheckConstraint("status <> 'active' OR expires_at IS NOT NULL", name="active_expiry"),
        CheckConstraint("NOT is_primary OR status = 'active'", name="primary_active"),
        Index(
            "uq_badge_affiliations_primary",
            "member_account_id",
            unique=True,
            postgresql_where=sa_text("is_primary"),
        ),
        Index("ix_badge_affiliations_org_status", "org_badge_id", "status"),
        Index(
            "ix_badge_affiliations_expires_at",
            "expires_at",
            postgresql_where=sa_text("status = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[uuid.UUID] = _public_id()
    member_account_id: Mapped[int] = mapped_column(
        ForeignKey("platform_accounts.id", ondelete="CASCADE")
    )
    org_badge_id: Mapped[int] = mapped_column(ForeignKey("badges.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(Text)
    is_primary: Mapped[bool] = mapped_column(server_default=sa_text("false"))
    title: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    wikidata_property: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[str] = mapped_column(Text)
    validated_by: Mapped[str | None] = mapped_column(Text)
    validated_at: Mapped[datetime | None]
    """Last validation or reconfirmation."""
    expires_at: Mapped[datetime | None]
    ended_by: Mapped[str | None] = mapped_column(Text)
    ended_at: Mapped[datetime | None]
    end_reason: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, server_default=sa_text("1"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class BadgeEvent(Base):
    """Append-only history: grants, revocations, blocks, affiliations, keys."""

    __tablename__ = "badge_events"
    __table_args__ = (check_in("action", BadgeAction, "action"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("platform_accounts.id", ondelete="CASCADE"), index=True
    )
    badge_id: Mapped[int | None] = mapped_column(
        ForeignKey("badges.id", ondelete="SET NULL"), index=True
    )
    affiliation_id: Mapped[int | None] = mapped_column(
        ForeignKey("badge_affiliations.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(Text)
    """Who did it (``staff:123@intercom``, ``organization:<uuid>``, ``system:badges``)."""
    source: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    changes: Mapped[dict[str, Any]] = mapped_column(server_default=sa_text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class BadgeRequest(Base):
    """Badge or affiliation request submitted from the website or by an organisation."""

    __tablename__ = "badge_requests"
    __table_args__ = (
        check_in("kind", RequestKind, "kind"),
        check_in("status", RequestStatus, "status"),
        CheckConstraint(
            "requested_category IS NULL OR requested_category IN (" + _CATEGORY_SQL + ")",
            name="requested_category",
        ),
        CheckConstraint(
            "kind <> 'affiliation' OR org_badge_id IS NOT NULL", name="affiliation_org"
        ),
        Index(
            "uq_badge_requests_pending",
            "kind",
            "account_id",
            sa_text("coalesce(org_badge_id, 0)"),
            unique=True,
            postgresql_where=sa_text("status = 'pending'"),
        ),
        Index("ix_badge_requests_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[uuid.UUID] = _public_id()
    kind: Mapped[str] = mapped_column(Text)
    account_id: Mapped[int] = mapped_column(ForeignKey("platform_accounts.id", ondelete="CASCADE"))
    requested_category: Mapped[str | None] = mapped_column(Text)
    org_badge_id: Mapped[int | None] = mapped_column(ForeignKey("badges.id", ondelete="CASCADE"))
    title: Mapped[str | None] = mapped_column(Text)
    justification: Mapped[str | None] = mapped_column(Text)
    contact: Mapped[str | None] = mapped_column(Text)
    """Contact given by the requester; read by the team only, never public."""
    evidence_urls: Mapped[list[str]] = mapped_column(JSONB, server_default=sa_text("'[]'::jsonb"))
    status: Mapped[str] = mapped_column(Text, server_default=sa_text("'pending'"))
    submitted_by: Mapped[str] = mapped_column(Text)
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None]
    decision_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class BadgeReport(Base):
    """Report of a wrong badge or affiliation. Nothing is stored about the reporter."""

    __tablename__ = "badge_reports"
    __table_args__ = (
        check_in("reason", ReportReason, "reason"),
        check_in("status", ReportStatus, "status"),
        Index("ix_badge_reports_status_created", "status", "created_at"),
        Index("ix_badge_reports_account_id", "account_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[uuid.UUID] = _public_id()
    account_id: Mapped[int] = mapped_column(ForeignKey("platform_accounts.id", ondelete="CASCADE"))
    reason: Mapped[str] = mapped_column(Text)
    comment: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=sa_text("'open'"))
    resolved_by: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None]
    resolution_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class BadgeOrgKey(Base):
    """API key of an organisation (manages its affiliates). Only the hash is stored."""

    __tablename__ = "badge_org_keys"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[uuid.UUID] = _public_id()
    org_badge_id: Mapped[int] = mapped_column(
        ForeignKey("badges.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    label: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    revoked_at: Mapped[datetime | None]


class BadgeWikidataLink(Base):
    """Affiliation statement read on Wikidata (member account → organisation item).

    Kept so that an affiliation can be proposed whichever of the two is imported first.
    """

    __tablename__ = "badge_wikidata_links"
    __table_args__ = (
        PrimaryKeyConstraint("member_account_id", "org_qid", "property"),
        Index("ix_badge_wikidata_links_org_qid", "org_qid"),
    )

    member_account_id: Mapped[int] = mapped_column(
        ForeignKey("platform_accounts.id", ondelete="CASCADE")
    )
    org_qid: Mapped[str] = mapped_column(Text)
    property: Mapped[str] = mapped_column(Text)
    seen_at: Mapped[datetime] = mapped_column(server_default=func.now())


class BadgeXCheck(Base):
    """X ``verified_type`` checks (government verification), one row per account."""

    __tablename__ = "badge_x_checks"
    __table_args__ = (
        Index(
            "ix_badge_x_checks_due",
            "hinted_at",
            postgresql_where=sa_text("checked_at IS NULL"),
        ),
    )

    account_id: Mapped[int] = mapped_column(
        ForeignKey("platform_accounts.id", ondelete="CASCADE"), primary_key=True
    )
    hinted_at: Mapped[datetime | None]
    """Last time the extension (or the team) asked for a check."""
    checked_at: Mapped[datetime | None]
    verified_type: Mapped[str | None] = mapped_column(Text)


class BadgeImportRun(Base):
    """Last Wikidata import of each scope (country QID or ``intl``)."""

    __tablename__ = "badge_import_runs"

    scope: Mapped[str] = mapped_column(Text, primary_key=True)
    started_at: Mapped[datetime | None]
    listed_at: Mapped[datetime | None]
    """End of the listing step (batches enqueued)."""
    accounts: Mapped[int] = mapped_column(Integer, server_default=sa_text("0"))
    batches: Mapped[int] = mapped_column(Integer, server_default=sa_text("0"))


class BadgeLogo(Base):
    """Organisation icon stored by Tower (content-addressed, immutable).

    Taken once from the organisation's official website and served by gate
    (``GET /v1/badges/logos/{sha256}.{ext}``), so that the extension never loads an image
    from the organisation's own site."""

    __tablename__ = "badge_logos"
    __table_args__ = (
        check_in("content_type", ICON_TYPES, "content_type"),
        CheckConstraint("octet_length(data) <= 200000", name="data_size"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256"),
    )

    sha256: Mapped[str] = mapped_column(Text, primary_key=True)
    content_type: Mapped[str] = mapped_column(Text)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    width: Mapped[int] = mapped_column(Integer, server_default=sa_text("0"))
    height: Mapped[int] = mapped_column(Integer, server_default=sa_text("0"))
    source_url: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
