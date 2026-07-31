from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.database.types import EnumString, StringArray
from app.models.enums import RemotePreference, SeniorityLevel

if TYPE_CHECKING:
    from app.models.match import JobMatch
    from app.models.notification import Notification


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Account holder.

    Single-user in practice today, but the table and its foreign keys exist now
    so that multi-tenancy is a scoping change rather than a schema migration
    over a table full of production job data.
    """

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(
        sa.String(320), nullable=False, unique=True, index=True
    )
    full_name: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)

    profile: Mapped[UserProfile | None] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )
    notifications: Mapped[list[Notification]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:
        return f"<User {self.email!r}>"


class UserProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """What the user is looking for.

    Read by both matchers: the rule matcher uses the hard constraints
    (``excluded_keywords``, locations, seniority floor) and the LLM matcher uses
    the whole thing as context for its judgement.
    """

    __tablename__ = "user_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    preferred_roles: Mapped[list[str]] = mapped_column(
        StringArray, nullable=False, default=list
    )
    preferred_locations: Mapped[list[str]] = mapped_column(
        StringArray, nullable=False, default=list
    )
    preferred_skills: Mapped[list[str]] = mapped_column(
        StringArray, nullable=False, default=list
    )
    industries: Mapped[list[str]] = mapped_column(
        StringArray, nullable=False, default=list
    )
    #: Hard veto. Any occurrence in a title or description disqualifies the
    #: posting outright, before scoring — this is the one rule that must never
    #: be overridden by a high semantic score.
    excluded_keywords: Mapped[list[str]] = mapped_column(
        StringArray, nullable=False, default=list
    )

    seniority: Mapped[SeniorityLevel] = mapped_column(
        EnumString(SeniorityLevel, 16), nullable=False, default=SeniorityLevel.UNKNOWN
    )
    remote_preference: Mapped[RemotePreference] = mapped_column(
        EnumString(RemotePreference, 16), nullable=False, default=RemotePreference.ANY
    )
    years_experience: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    #: How many levels above your own you are willing to see. 1 allows a
    #: stretch role; 0 is exact-level only. Applied as a hard filter, because
    #: "8+ years required" is a wall, not a scoring nudge.
    max_seniority_gap: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    requires_visa_sponsorship: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False
    )

    desired_salary_min: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    salary_currency: Mapped[str | None] = mapped_column(sa.String(8), nullable=True)

    #: Per-user override of ``settings.match_threshold``.
    match_threshold: Mapped[float] = mapped_column(
        sa.Float, nullable=False, default=0.6
    )
    #: When true, postings whose location could not be parsed are kept rather
    #: than dropped. Many boards write locations no parser will ever handle.
    include_unknown_location: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True
    )

    user: Mapped[User] = relationship(back_populates="profile")
    matches: Mapped[list[JobMatch]] = relationship(
        back_populates="profile", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:
        return f"<UserProfile roles={self.preferred_roles}>"
