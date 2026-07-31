from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.database.types import EnumString
from app.models.enums import ExtractionTier, ScrapeStatus, ScrapingStrategy

if TYPE_CHECKING:
    from app.models.company import Company


class ScrapeRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Per-scan telemetry.

    Prometheus counters answer "how is the fleet doing right now"; this table
    answers "what happened to *this* company last Tuesday", which is the
    question you actually have when a selector silently rots. Keeping
    per-company detail here instead of in metric labels is what keeps
    Prometheus cardinality bounded (see ``core/metrics.py``).
    """

    __tablename__ = "scrape_runs"
    __table_args__ = (
        sa.Index("ix_scrape_runs_company_time", "company_id", "started_at"),
        sa.Index("ix_scrape_runs_status", "status", "started_at"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True),
        sa.ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    started_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    status: Mapped[ScrapeStatus] = mapped_column(
        EnumString(ScrapeStatus, 16), nullable=False, default=ScrapeStatus.RUNNING
    )

    strategy_used: Mapped[ScrapingStrategy | None] = mapped_column(
        EnumString(ScrapingStrategy, 32), nullable=True
    )
    extraction_tier: Mapped[ExtractionTier | None] = mapped_column(
        EnumString(ExtractionTier, 32), nullable=True
    )
    selector_version: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    confidence: Mapped[float | None] = mapped_column(sa.Float, nullable=True)

    jobs_found: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    jobs_new: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    jobs_duplicate: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    notifications_sent: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0
    )

    fetch_ms: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    render_ms: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    total_ms: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)

    llm_calls: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    llm_tokens_in: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    llm_tokens_out: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    llm_cost_usd: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)

    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    company: Mapped[Company] = relationship(back_populates="scrape_runs")

    def __repr__(self) -> str:
        return f"<ScrapeRun {self.status} found={self.jobs_found} new={self.jobs_new}>"
