"""Types PostgreSQL non couverts par SQLAlchemy."""

from __future__ import annotations

from typing import Any

from sqlalchemy.types import UserDefinedType


class XID8(UserDefinedType[int]):
    """Identifiant de transaction 64 bits (``pg_current_xact_id()``)."""

    cache_ok = True

    def get_col_spec(self, **_: Any) -> str:
        return "xid8"
