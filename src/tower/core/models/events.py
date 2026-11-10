from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Identity, Index, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from tower.core.db.base import Base
from tower.core.db.types import XID8


class Event(Base):
    """Outbox transactionnelle : écrite dans la même transaction que le changement d'état.

    ``xid`` est l'identifiant de la transaction d'écriture. Les lecteurs parcourent la
    table dans l'ordre ``(xid, id)`` et ignorent les transactions encore ouvertes, ce
    qui garantit qu'aucun événement n'est sauté (voir ``tower.core.events.outbox``).
    """

    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_xid_id", "xid", "id"),
        Index("ix_events_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    xid: Mapped[int] = mapped_column(XID8(), server_default=text("pg_current_xact_id()"))
    topic: Mapped[str] = mapped_column(Text)
    aggregate_type: Mapped[str | None] = mapped_column(Text)
    aggregate_id: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    actor: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class EventCursor(Base):
    """Position d'un consommateur nommé dans l'outbox (reprise après redémarrage)."""

    __tablename__ = "event_cursors"

    consumer: Mapped[str] = mapped_column(Text, primary_key=True)
    last_xid: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    last_id: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
