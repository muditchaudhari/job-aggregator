"""Portable column types.

Production runs on PostgreSQL and wants ``JSONB`` and native ``text[]``. The
unit test suite runs on in-memory SQLite, which has neither. Declaring the
Postgres type with a SQLite variant means one set of model definitions serves
both, instead of the usual choice between "tests need a live Postgres" and
"models use lowest-common-denominator types in production".
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Generic, TypeVar

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

#: ``jsonb`` on PostgreSQL, ``json`` text on SQLite.
JSONB = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")

#: ``text[]`` on PostgreSQL, a JSON array on SQLite.
StringArray = postgresql.ARRAY(sa.Text()).with_variant(sa.JSON(), "sqlite")

EnumT = TypeVar("EnumT", bound=StrEnum)


class EnumString(TypeDecorator, Generic[EnumT]):
    """A ``StrEnum`` stored as a plain ``VARCHAR``, and read back as the enum.

    Storing enums as strings rather than native PostgreSQL enums is deliberate
    (see ``models/enums.py``): adding a value should be a code change, not an
    ``ALTER TYPE`` that locks the table.

    But a bare ``String`` column annotated ``Mapped[SomeEnum]`` only *looks*
    typed. SQLAlchemy hands back whatever the driver returned, so every object
    loaded from the database carries plain strings — and then
    ``profile.seniority.rank`` raises ``AttributeError`` inside a Celery
    worker while passing every test that happens to use an object it just
    constructed in memory. This decorator closes that gap: the value is an
    enum member on the way in and on the way out.

    The DDL is unchanged (still ``VARCHAR(n)``), so no migration is required.
    """

    impl = sa.String
    cache_ok = True

    def __init__(self, enum_class: type[EnumT], length: int = 32, **kwargs: Any) -> None:
        self.enum_class = enum_class
        super().__init__(length=length, **kwargs)

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, self.enum_class):
            return value.value
        # Coerce and validate: a typo'd string fails here, at the write, rather
        # than becoming a row nothing can ever match.
        return self.enum_class(value).value

    def process_result_value(self, value: Any, dialect: Dialect) -> EnumT | str | None:
        if value is None:
            return None
        try:
            return self.enum_class(value)
        except ValueError:
            # A value written by a newer version of the code. Returning the raw
            # string degrades one comparison; raising would fail the whole scan.
            return value
