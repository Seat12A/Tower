from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from sqlalchemy import CheckConstraint


def check_in(column: str, values: Iterable[str] | type[StrEnum], name: str) -> CheckConstraint:
    """Contrainte ``CHECK (column IN (...))`` : on préfère TEXT + CHECK aux enums PG."""
    items = [v.value if isinstance(v, StrEnum) else v for v in values]
    quoted = ", ".join(f"'{v}'" for v in items)
    return CheckConstraint(f"{column} IN ({quoted})", name=name)
