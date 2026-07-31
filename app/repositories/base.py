"""Generic repository.

The repository layer exists so that services can be tested against a fake
without a database, and so that query construction lives in one place per
aggregate instead of being scattered through endpoints and Celery tasks.

Repositories never commit. The caller owns the transaction boundary — a scan
that inserts jobs, records matches, and writes a run row must either land
entirely or not at all, and that decision cannot be made from inside a
repository method.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, Generic, TypeVar

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.database.base import Base

ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    model: type[ModelT]

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, entity_id: uuid.UUID) -> ModelT | None:
        return self.session.get(self.model, entity_id)

    def list(self, *, limit: int = 100, offset: int = 0) -> Sequence[ModelT]:
        stmt = sa.select(self.model).limit(limit).offset(offset)
        return self.session.execute(stmt).scalars().all()

    def count(self) -> int:
        stmt = sa.select(sa.func.count()).select_from(self.model)
        return int(self.session.execute(stmt).scalar_one())

    def add(self, entity: ModelT) -> ModelT:
        self.session.add(entity)
        # Flush, not commit: the caller may still roll back, but downstream
        # code in the same unit of work needs the generated defaults now.
        self.session.flush()
        return entity

    def add_all(self, entities: Sequence[ModelT]) -> Sequence[ModelT]:
        self.session.add_all(entities)
        self.session.flush()
        return entities

    def delete(self, entity: ModelT) -> None:
        self.session.delete(entity)
        self.session.flush()

    def update(self, entity: ModelT, **fields: Any) -> ModelT:
        for key, value in fields.items():
            setattr(entity, key, value)
        self.session.flush()
        return entity
