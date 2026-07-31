from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.database.types import JSONB, EnumString
from app.models.enums import SelectorOrigin, SelectorStrategy


class Selector(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A learned extraction strategy for one website.

    Rows are immutable in their selector fields (AD-4): the learner inserts
    version *n+1* and deactivates *n*. Only the counters and ``is_active`` are
    ever updated, so a version's historical success rate stays truthful.

    Keyed by ``website`` — a registrable domain — not by company. Two companies
    on the same white-labelled board share one learned strategy, and the second
    one costs no LLM call at all.
    """

    __tablename__ = "selectors"
    __table_args__ = (
        sa.UniqueConstraint(
            "website", "selector_version", name="uq_selectors_website_version"
        ),
        sa.Index("ix_selectors_active", "website", "is_active"),
        sa.CheckConstraint(
            "confidence_score >= 0 AND confidence_score <= 1",
            name="confidence_in_range",
        ),
    )

    website: Mapped[str] = mapped_column(sa.String(255), nullable=False, index=True)
    selector_version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    strategy: Mapped[SelectorStrategy] = mapped_column(
        EnumString(SelectorStrategy, 16), nullable=False, default=SelectorStrategy.CSS
    )

    #: Selects the repeating element that wraps one posting. Everything else is
    #: resolved relative to it, which is what makes a selector set survive
    #: layout changes outside the listing itself.
    container_selector: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    title_selector: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    url_selector: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    location_selector: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    description_selector: Mapped[str | None] = mapped_column(
        sa.String(512), nullable=True
    )
    date_selector: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    department_selector: Mapped[str | None] = mapped_column(
        sa.String(512), nullable=True
    )

    #: For ``JSON_PATH`` strategy: where the array lives in an embedded blob and
    #: which keys map to which field.
    json_config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    #: Whether this site needs a headless render for the selectors to resolve.
    requires_render: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False
    )

    confidence_score: Mapped[float] = mapped_column(
        sa.Float, nullable=False, default=0.0
    )
    success_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    failure_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    #: Reset on success. Drives the regeneration trigger, so a site that fails
    #: once a week and recovers is never needlessly relearned.
    consecutive_failures: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0
    )

    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    origin: Mapped[SelectorOrigin] = mapped_column(
        EnumString(SelectorOrigin, 16), nullable=False, default=SelectorOrigin.LLM
    )
    llm_model: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    notes: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.failure_count
        return self.success_count / total if total else 0.0

    def record_success(self, confidence: float) -> None:
        self.success_count += 1
        self.consecutive_failures = 0
        # Exponential moving average rather than last-value: one unusually
        # clean scrape should not paper over a selector that is mostly shaky.
        self.confidence_score = round(
            0.7 * self.confidence_score + 0.3 * confidence, 4
        )

    def record_failure(self) -> None:
        self.failure_count += 1
        self.consecutive_failures += 1
        self.confidence_score = round(max(0.0, self.confidence_score * 0.7), 4)

    def __repr__(self) -> str:
        return (
            f"<Selector {self.website} v{self.selector_version} "
            f"conf={self.confidence_score:.2f} active={self.is_active}>"
        )
