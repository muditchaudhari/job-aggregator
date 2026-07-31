from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.database.types import StringArray

if TYPE_CHECKING:
    from app.models.job import Job
    from app.models.user import UserProfile


class JobMatch(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The result of scoring one job against one profile.

    Persisted rather than recomputed because it is the audit trail for every
    notification: when the user asks "why did you send me this?", the answer
    has to be the reasoning that was actually used at the time, not what the
    current model would say today. ``matcher_version`` makes a change in
    matching logic visible in the data.
    """

    __tablename__ = "job_matches"
    __table_args__ = (
        sa.UniqueConstraint("job_id", "profile_id", name="uq_job_matches_job_profile"),
        sa.Index("ix_job_matches_score", "score"),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True),
        sa.ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True),
        sa.ForeignKey("user_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    score: Mapped[float] = mapped_column(sa.Float, nullable=False)
    matched_skills: Mapped[list[str]] = mapped_column(
        StringArray, nullable=False, default=list
    )
    missing_skills: Mapped[list[str]] = mapped_column(
        StringArray, nullable=False, default=list
    )
    reasoning: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    #: ``rule`` or ``llm`` — which matcher produced the final score.
    matcher: Mapped[str] = mapped_column(sa.String(32), nullable=False, default="rule")
    matcher_version: Mapped[str] = mapped_column(
        sa.String(32), nullable=False, default="1"
    )
    is_match: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)

    job: Mapped[Job] = relationship(back_populates="matches")
    profile: Mapped[UserProfile] = relationship(back_populates="matches")

    def __repr__(self) -> str:
        return f"<JobMatch score={self.score:.2f} match={self.is_match}>"
