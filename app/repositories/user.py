from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.orm import selectinload

from app.models.user import User, UserProfile
from app.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    model = User

    def get_by_email(self, email: str) -> User | None:
        stmt = sa.select(User).where(User.email == email.lower())
        return self.session.execute(stmt).scalar_one_or_none()

    def get_or_create(self, email: str, full_name: str | None = None) -> User:
        existing = self.get_by_email(email)
        if existing:
            return existing
        return self.add(User(email=email.lower(), full_name=full_name))

    def list_with_profiles(self) -> Sequence[User]:
        """Active users and their profiles, eagerly loaded.

        ``selectinload`` rather than lazy access: the notification fan-out
        iterates every user and touches every profile, which is a textbook
        N+1 if left to the default.
        """
        stmt = (
            sa.select(User)
            .where(User.is_active.is_(True))
            .options(selectinload(User.profile))
        )
        return self.session.execute(stmt).scalars().all()


class UserProfileRepository(BaseRepository[UserProfile]):
    model = UserProfile

    def get_for_user(self, user_id: uuid.UUID) -> UserProfile | None:
        stmt = sa.select(UserProfile).where(UserProfile.user_id == user_id)
        return self.session.execute(stmt).scalar_one_or_none()

    def list_all(self) -> Sequence[UserProfile]:
        stmt = sa.select(UserProfile).options(selectinload(UserProfile.user))
        return self.session.execute(stmt).scalars().all()
