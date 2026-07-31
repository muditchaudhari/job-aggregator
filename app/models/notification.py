from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.database.types import EnumString
from app.models.enums import NotificationChannel, NotificationStatus

if TYPE_CHECKING:
    from app.models.job import Job
    from app.models.user import User


class Notification(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One delivery attempt of one job to one user on one channel.

    The unique constraint across ``(job_id, user_id, channel)`` is the
    idempotency guarantee: a retried Celery task, a manual rescan, or a second
    worker cannot produce a duplicate ping about the same posting. Deliveries
    that fail keep the row and the error, so retries are driven from data
    rather than from a queue that may have already been drained.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        sa.UniqueConstraint(
            "job_id", "user_id", "channel", name="uq_notifications_job_user_channel"
        ),
        sa.Index("ix_notifications_status", "status", "created_at"),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True),
        sa.ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    channel: Mapped[NotificationChannel] = mapped_column(
        EnumString(NotificationChannel, 16), nullable=False
    )
    status: Mapped[NotificationStatus] = mapped_column(
        EnumString(NotificationStatus, 16), nullable=False, default=NotificationStatus.PENDING
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    #: Denormalised from the match so the delivery log stands alone in the UI
    #: without a three-table join on every row.
    match_score: Mapped[float | None] = mapped_column(sa.Float, nullable=True)

    job: Mapped[Job] = relationship(back_populates="notifications")
    user: Mapped[User] = relationship(back_populates="notifications")

    def __repr__(self) -> str:
        return f"<Notification {self.channel} {self.status}>"
