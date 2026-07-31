from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.database.types import JSONB, EnumString
from app.models.enums import ATSType, ScrapeFrequency, ScrapingStrategy
from app.utils.time import utcnow

if TYPE_CHECKING:
    from app.models.job import Job
    from app.models.scrape_run import ScrapeRun


class Company(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A registered career page.

    ``ats_type`` and ``scraping_strategy`` start as ``unknown``/``auto`` and are
    filled in by the detector on first scan, then cached here. Re-detection only
    happens when extraction starts failing — sites change ATS rarely, and
    detecting on every scan would cost an extra request per company per run.
    """

    __tablename__ = "companies"
    __table_args__ = (
        sa.Index("ix_companies_due", "is_active", "next_scrape_at"),
        sa.CheckConstraint(
            "scrape_interval_minutes > 0", name="scrape_interval_positive"
        ),
    )

    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    career_url: Mapped[str] = mapped_column(
        sa.String(1024), nullable=False, unique=True, index=True
    )
    #: Registrable domain, denormalised from ``career_url`` so it can be joined
    #: against ``selectors.website`` and used as the rate-limit bucket key.
    website: Mapped[str] = mapped_column(sa.String(255), nullable=False, index=True)

    ats_type: Mapped[ATSType] = mapped_column(
        EnumString(ATSType, 32), nullable=False, default=ATSType.UNKNOWN
    )
    scraping_strategy: Mapped[ScrapingStrategy] = mapped_column(
        EnumString(ScrapingStrategy, 32), nullable=False, default=ScrapingStrategy.AUTO
    )
    #: Board identifier for API-backed platforms — the ``acme`` in
    #: ``boards.greenhouse.io/acme``. Populated by detection.
    board_token: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    #: Resolved JSON endpoint, when tier 1 applies.
    api_endpoint: Mapped[str | None] = mapped_column(sa.String(1024), nullable=True)

    scrape_frequency: Mapped[ScrapeFrequency] = mapped_column(
        EnumString(ScrapeFrequency, 16), nullable=False, default=ScrapeFrequency.DAILY
    )
    scrape_interval_minutes: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=1440
    )
    last_scraped_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    next_scrape_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True, index=True
    )

    consecutive_failures: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0
    )
    last_error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)

    #: Per-company overrides — extra HTTP headers, pagination hints, a manual
    #: API endpoint. Kept as JSON because the shape differs per ATS and pinning
    #: it into columns would mean a migration per integration quirk.
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    jobs: Mapped[list[Job]] = relationship(
        back_populates="company", cascade="all, delete-orphan", passive_deletes=True
    )
    scrape_runs: Mapped[list[ScrapeRun]] = relationship(
        back_populates="company", cascade="all, delete-orphan", passive_deletes=True
    )

    def schedule_next(self, *, failed: bool = False) -> None:
        """Advance the scrape clock.

        Failures back off exponentially (capped at a week) rather than retrying
        on the normal cadence. A site that has been broken for a day is not
        likely to be fixed in the next hour, and hammering it neither helps us
        nor endears us to them.
        """
        now = utcnow()
        self.last_scraped_at = now
        if failed:
            self.consecutive_failures += 1
            backoff = min(
                self.scrape_interval_minutes * (2**self.consecutive_failures),
                10_080,
            )
            self.next_scrape_at = now + timedelta(minutes=backoff)
        else:
            self.consecutive_failures = 0
            self.last_error = None
            self.next_scrape_at = now + timedelta(minutes=self.scrape_interval_minutes)

    def __repr__(self) -> str:
        return f"<Company {self.name!r} ats={self.ats_type}>"
