"""``halfvec`` column type of pgvector without the ``pgvector`` Python package (which
pulls numpy: RAM is billed). Vectors cross the wire as text (``'[0.1,0.2,…]'``): the
bind is cast to ``halfvec(n)`` in SQL and the column is read back as text.

Python side, a vector is a ``list[float]``. Similarity search stays in PostgreSQL
(``<=>``, cosine distance); exact comparisons on small sets use
``tower.core.domain.factcheck.vectors``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.types import UserDefinedType

__all__ = ["EMBEDDING_DIMS", "HalfVec", "format_vector", "parse_vector", "vector_param"]

EMBEDDING_DIMS = 512


def format_vector(values: Sequence[float]) -> str:
    """``[0.1,0.2]`` text form (repr of float keeps full precision; halfvec rounds)."""
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


def parse_vector(text: str | None) -> list[float] | None:
    if text is None:
        return None
    body = text.strip()[1:-1]
    return [float(x) for x in body.split(",")] if body else []


class HalfVec(UserDefinedType[list[float]]):
    cache_ok = True

    def __init__(self, dims: int = EMBEDDING_DIMS) -> None:
        self.dims = dims

    def get_col_spec(self, **kw: Any) -> str:
        return f"halfvec({self.dims})"

    def bind_processor(self, dialect: Any) -> Callable[[Any], str | None]:
        def process(value: Any) -> str | None:
            if value is None:
                return None
            if isinstance(value, str):
                return value
            return format_vector(value)

        return process

    def result_processor(self, dialect: Any, coltype: Any) -> Callable[[Any], list[float] | None]:
        def process(value: Any) -> list[float] | None:
            return parse_vector(value)

        return process

    def bind_expression(self, bindvalue: Any) -> ColumnElement[Any]:
        return _TypedCast(bindvalue, self)

    def column_expression(self, colexpr: Any) -> ColumnElement[Any]:
        return _TextOf(colexpr, self)


class _TypedCast(ColumnElement[Any]):
    """``CAST(CAST(:p AS TEXT) AS halfvec(n))``: the inner cast makes asyncpg send the
    parameter as text (it has no codec for halfvec)."""

    inherit_cache = True

    def __init__(self, inner: Any, type_: HalfVec) -> None:
        self.inner = inner
        self.type = type_


class _TextOf(ColumnElement[Any]):
    """``CAST(col AS TEXT)``: read a vector back as its text form."""

    inherit_cache = True

    def __init__(self, inner: Any, type_: HalfVec) -> None:
        self.inner = inner
        self.type = type_


@compiles(_TypedCast)
def _compile_cast(element: _TypedCast, compiler: Any, **kw: Any) -> str:
    inner = compiler.process(element.inner, **kw)
    spec = HalfVec.get_col_spec(element.type)  # type: ignore[arg-type]
    return f"CAST(CAST({inner} AS TEXT) AS {spec})"


@compiles(_TextOf)
def _compile_text(element: _TextOf, compiler: Any, **kw: Any) -> str:
    return f"CAST({compiler.process(element.inner, **kw)} AS TEXT)"


def vector_param(values: Sequence[float]) -> str:
    """Text form for raw SQL: ``CAST(:q AS halfvec(512))`` with ``q=vector_param(v)``."""
    return format_vector(values)
