from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Identity, LargeBinary, Text, func, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from tower.core.db.base import Base


class ServiceClient(Base):
    """Client machine (bot intercom, scripts) authentifié par token de service.

    Seule l'empreinte SHA-256 du token est stockée ; le token n'est affiché qu'une fois.
    """

    __tablename__ = "service_clients"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    revoked_at: Mapped[datetime | None]


class StaffMember(Base):
    """Membre de l'équipe, identifié par son ID Discord, avec ses rôles."""

    __tablename__ = "staff_members"

    discord_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    display_name: Mapped[str | None] = mapped_column(Text)
    roles: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    active: Mapped[bool] = mapped_column(server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
