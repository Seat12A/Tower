from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Identity, LargeBinary, Text, func, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from tower.core.db.base import Base


class ServiceClient(Base):
    """Machine client (intercom bot, scripts) authenticated by service token.

    Only the SHA-256 fingerprint of the token is stored; the token is shown only once.
    """

    __tablename__ = "service_clients"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    revoked_at: Mapped[datetime | None]


class StaffMember(Base):
    """Team member, identified by their Discord ID, with their roles."""

    __tablename__ = "staff_members"

    discord_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    display_name: Mapped[str | None] = mapped_column(Text)
    roles: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    active: Mapped[bool] = mapped_column(server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
