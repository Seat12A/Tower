"""PostgreSQL types not covered by SQLAlchemy."""

from __future__ import annotations

from typing import Any

from sqlalchemy.types import UserDefinedType


class XID8(UserDefinedType[int]):
    """64-bit transaction identifier (``pg_current_xact_id()``)."""

    cache_ok = True

    def get_col_spec(self, **_: Any) -> str:
        return "xid8"
