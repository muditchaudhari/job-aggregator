from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.api.deps import DbSession, TransactionalSession
from app.core.logging import get_logger
from app.models.user import UserProfile
from app.repositories.user import UserProfileRepository, UserRepository
from app.schemas.profile import ProfileRead, ProfileUpdate, ProfileWrite

logger = get_logger(__name__)
router = APIRouter(prefix="/profile", tags=["profile"])


@router.post("", response_model=ProfileRead, status_code=status.HTTP_201_CREATED)
def create_profile(payload: ProfileWrite, session: TransactionalSession) -> ProfileRead:
    """Create or replace the profile for an email address.

    Idempotent by email: posting twice updates rather than erroring. Profiles
    are typically written by a setup script or a config file, and making the
    second run fail would be a papercut with no upside.
    """
    users = UserRepository(session)
    profiles = UserProfileRepository(session)

    user = users.get_or_create(payload.email, payload.full_name)
    if payload.full_name:
        user.full_name = payload.full_name

    existing = profiles.get_for_user(user.id)
    fields = payload.model_dump(exclude={"email", "full_name"})

    if existing is not None:
        profiles.update(existing, **fields)
        logger.info("profile.replaced", email=payload.email)
        return ProfileRead.model_validate(existing)

    profile = profiles.add(UserProfile(user_id=user.id, **fields))
    logger.info("profile.created", email=payload.email)
    return ProfileRead.model_validate(profile)


@router.get("", response_model=ProfileRead)
def read_profile(session: DbSession) -> ProfileRead:
    profiles = UserProfileRepository(session).list_all()
    if not profiles:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no profile configured",
        )
    return ProfileRead.model_validate(profiles[0])


@router.put("", response_model=ProfileRead)
def update_profile(payload: ProfileUpdate, session: TransactionalSession) -> ProfileRead:
    """Partial update. Unset fields are left untouched.

    ``exclude_unset`` rather than ``exclude_none`` — the difference matters,
    because clearing a list ("I no longer exclude any keywords") is a
    legitimate update that ``exclude_none`` would silently discard.
    """
    repository = UserProfileRepository(session)
    profiles = repository.list_all()
    if not profiles:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no profile configured; create one with POST /profile first",
        )

    profile = profiles[0]
    updates = payload.model_dump(exclude_unset=True)

    full_name = updates.pop("full_name", None)
    if full_name is not None and profile.user is not None:
        profile.user.full_name = full_name

    if updates:
        repository.update(profile, **updates)

    logger.info("profile.updated", fields=sorted(updates))
    return ProfileRead.model_validate(profile)
