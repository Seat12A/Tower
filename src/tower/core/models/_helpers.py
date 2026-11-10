from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from sqlalchemy import CheckConstraint


def check_in(column: str, values: Iterable[str] | type[StrEnum], name: str) -> CheckConstraint:
    """``CHECK (column IN (...))`` constraint: we prefer TEXT + CHECK over PG enums."""
    items = [v.value if isinstance(v, StrEnum) else v for v in values]
    quoted = ", ".join(f"'{v}'" for v in items)
    return CheckConstraint(f"{column} IN ({quoted})", name=name)
