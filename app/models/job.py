from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.database.types import JSONB, EnumString, StringArray
from app.models.enums import (
    EmploymentType,
    ExtractionTier,
    RemoteType,
    SalaryPeriod,
    SeniorityLevel,
)

if TYPE_CHECKING:
    from app.models.company import Company
    from app.models.match import JobMatch
    from app.models.notification import Notification


class Job(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A normalised job posting.

    ``content_hash`` is the deduplication key (AD-6) and is enforced by a unique
    constraint scoped to the company, so concurrent scans of the same board
    cannot both insert the same posting.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        sa.UniqueConstraint("company_id", "content_hash", name="uq_jobs_company_hash"),
        sa.Index("ix_jobs_first_seen", "first_seen_at"),
        sa.Index("ix_jobs_company_active", "company_id", "is_active"),
        sa.Index("ix_jobs_posted", "posted_date"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True),
        sa.ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: The platform's own requisition id when one is exposed. The most reliable
    #: identity signal available, and the reason tier 1 beats every other tier.
    external_job_id: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)

    title: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    url: Mapped[str] = mapped_column(sa.String(2048), nullable=False)

    # Location: the raw string is preserved alongside the parsed components,
    # because normalisation is lossy and occasionally wrong, and a human
    # debugging a bad match needs to see what the site actually said.
    location_raw: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    location_city: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    location_region: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    location_country: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    remote_type: Mapped[RemoteType] = mapped_column(
        EnumString(RemoteType, 16), nullable=False, default=RemoteType.UNKNOWN
    )

    employment_type: Mapped[EmploymentType] = mapped_column(
        EnumString(EmploymentType, 16), nullable=False, default=EmploymentType.UNKNOWN
    )
    seniority: Mapped[SeniorityLevel] = mapped_column(
        EnumString(SeniorityLevel, 16), nullable=False, default=SeniorityLevel.UNKNOWN
    )

    description: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    requirements: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    #: Skills detected in the description, used by the rule matcher without
    #: re-parsing the full text on every profile comparison.
    detected_skills: Mapped[list[str]] = mapped_column(
        StringArray, nullable=False, default=list
    )

    salary_min: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    salary_max: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    salary_currency: Mapped[str | None] = mapped_column(sa.String(8), nullable=True)
    salary_period: Mapped[SalaryPeriod] = mapped_column(
        EnumString(SalaryPeriod, 16), nullable=False, default=SalaryPeriod.UNKNOWN
    )
    salary_raw: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)

    posted_date: Mapped[date | None] = mapped_column(sa.Date, nullable=True)
    #: The full timestamp when the source gave one. Kept alongside the date
    #: because "posted 4 hours ago" and "posted today" are different answers
    #: to a job hunter, and a Date column silently throws the difference away.
    #: Null when the board only published a day (or nothing at all).
    posted_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    content_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)

    #: Untouched upstream payload. Retained so a future extractor improvement
    #: can be backfilled over history without re-scraping (see ARCHITECTURE §9).
    raw_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    #: Which rung of the ladder produced this row — the raw material for
    #: "is our learned selector still earning its keep?"
    extraction_tier: Mapped[ExtractionTier] = mapped_column(
        EnumString(ExtractionTier, 32), nullable=False, default=ExtractionTier.CSS_SELECTOR
    )

    first_seen_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    #: Cleared when a posting stops appearing on the board — kept rather than
    #: deleted so that "how long was this open?" stays answerable.
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)

    company: Mapped[Company] = relationship(back_populates="jobs")
    matches: Mapped[list[JobMatch]] = relationship(
        back_populates="job", cascade="all, delete-orphan", passive_deletes=True
    )
    notifications: Mapped[list[Notification]] = relationship(
        back_populates="job", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:
        return f"<Job {self.title!r} @ {self.location_raw!r}>"
