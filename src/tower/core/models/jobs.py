from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import BigInteger, Identity, Index, SmallInteger, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from tower.core.db.base import Base
from tower.core.models._helpers import check_in


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Job(Base):
    """File de travaux PostgreSQL (``FOR UPDATE SKIP LOCKED``)."""

    __tablename__ = "jobs"
    __table_args__ = (
        check_in("status", JobStatus, "status"),
        Index(
            "ix_jobs_ready",
            "queue",
            text("priority DESC"),
            "run_at",
            "id",
            postgresql_where=text("status = 'queued'"),
        ),
        Index("ix_jobs_lease", "locked_until", postgresql_where=text("status = 'running'")),
        Index(
            "uq_jobs_dedupe_active",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        Index(
            "ix_jobs_finished",
            "finished_at",
            postgresql_where=text("status IN ('succeeded', 'failed', 'cancelled')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    queue: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    priority: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'queued'"))
    attempts: Mapped[int] = mapped_column(server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(server_default=text("5"))
    run_at: Mapped[datetime] = mapped_column(server_default=func.now())
    locked_by: Mapped[str | None] = mapped_column(Text)
    locked_until: Mapped[datetime | None]
    dedupe_key: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
