from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Identity, Index, SmallInteger, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from tower.core.db.base import Base
from tower.core.domain.dossiers import DossierKind, DossierStatus
from tower.core.models._helpers import check_in


class Dossier(Base):
    """Verification or moderation dossier concerning an X post."""

    __tablename__ = "dossiers"
    __table_args__ = (
        check_in("kind", DossierKind, "kind"),
        check_in("status", DossierStatus, "status"),
        Index("ix_dossiers_status_kind_created", "status", "kind", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    public_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, server_default=text("gen_random_uuid()")
    )
    kind: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    x_post_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    title: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    assigned_to: Mapped[int | None] = mapped_column(BigInteger)
    """Discord ID of the verifier who took the dossier for review."""
    published_url: Mapped[str | None] = mapped_column(Text)
    opened_by: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012


class DossierEvent(Base):
    """Immutable audit log: one row per transition."""

    __tablename__ = "dossier_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    dossier_id: Mapped[int] = mapped_column(
        ForeignKey("dossiers.id", ondelete="RESTRICT"), index=True
    )
    from_status: Mapped[str | None] = mapped_column(Text)
    to_status: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
