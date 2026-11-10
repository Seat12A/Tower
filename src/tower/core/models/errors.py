from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Index, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from tower.core.db.base import Base
from tower.core.domain.error_codes import Severity
from tower.core.models._helpers import check_in


class ErrorReport(Base):
    """Stored errors, aggregated by fingerprint (code + exception type + location).

    One row per *distinct* error, not per occurrence: the table stays small even if an
    error repeats thousands of times. ``occurrences`` counts, ``last_*`` keeps the latest
    example. A resolved error that reappears is reopened.
    """

    __tablename__ = "error_reports"
    __table_args__ = (
        check_in("severity", Severity, "severity"),
        Index("ix_error_reports_open", "last_seen", postgresql_where=text("resolved_at IS NULL")),
        Index("ix_error_reports_code", "code"),
    )

    fingerprint: Mapped[str] = mapped_column(Text, primary_key=True)
    code: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    process: Mapped[str] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    exception_type: Mapped[str | None] = mapped_column(Text)
    last_message: Mapped[str | None] = mapped_column(Text)
    last_traceback: Mapped[str | None] = mapped_column(Text)
    last_context: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    occurrences: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    first_seen: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen: Mapped[datetime] = mapped_column(server_default=func.now())
    resolved_at: Mapped[datetime | None]
    resolved_by: Mapped[str | None] = mapped_column(Text)
    resolution_note: Mapped[str | None] = mapped_column(Text)
