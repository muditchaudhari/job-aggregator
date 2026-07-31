"""FastAPI dependencies.

Endpoints depend on these rather than constructing repositories and services
themselves, so a test can override one line and swap in a fake without
monkeypatching module internals.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.models.user import UserProfile
from app.repositories.user import UserProfileRepository
from app.schemas.common import PaginationParams

DbSession = Annotated[Session, Depends(get_db)]


def pagination(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> PaginationParams:
    return PaginationParams(limit=limit, offset=offset)


Pagination = Annotated[PaginationParams, Depends(pagination)]


def get_active_profile(session: DbSession) -> UserProfile:
    """The profile that scoping and match scores are relative to.

    Single-user today: the first (and only) profile. The seam is here rather
    than scattered through endpoints, so introducing authentication later means
    changing this function and nothing else — every endpoint already asks "who
    is the caller?" through it.
    """
    profiles = UserProfileRepository(session).list_all()
    if not profiles:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No profile configured. Create one with POST /api/v1/profile.",
        )
    return profiles[0]


ActiveProfile = Annotated[UserProfile, Depends(get_active_profile)]


def get_optional_profile(session: DbSession) -> UserProfile | None:
    """Profile if one exists, ``None`` otherwise.

    Used by listing endpoints, which should still return jobs on a fresh
    install where no profile has been created yet — just without match scores.
    """
    profiles = UserProfileRepository(session).list_all()
    return profiles[0] if profiles else None


OptionalProfile = Annotated[UserProfile | None, Depends(get_optional_profile)]


def transactional(session: DbSession) -> Iterator[Session]:
    """Commit on a clean exit for endpoints that mutate state.

    ``get_db`` deliberately does not commit (see ``database/session.py``); this
    makes the transaction boundary explicit in an endpoint's signature rather
    than implied by whether it happened to return a 2xx.
    """
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise


TransactionalSession = Annotated[Session, Depends(transactional)]
