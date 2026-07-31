from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import selectinload

from app.models.enums import NotificationChannel, NotificationStatus
from app.models.notification import Notification
from app.repositories.base import BaseRepository


class NotificationRepository(BaseRepository[Notification]):
    model = Notification

    def already_notified(
        self, job_id: uuid.UUID, user_id: uuid.UUID, channel: NotificationChannel
    ) -> bool:
        """Has this job already been delivered on this channel?

        Cheap pre-check in front of the unique constraint. The constraint is
        what actually guarantees correctness; this just avoids the cost of
        rendering an email body that would be thrown away.
        """
        stmt = sa.select(sa.func.count()).select_from(Notification).where(
            Notification.job_id == job_id,
            Notification.user_id == user_id,
            Notification.channel == channel,
            Notification.status != NotificationStatus.FAILED,
        )
        return int(self.session.execute(stmt).scalar_one()) > 0

    def list_filtered(
        self,
        *,
        user_id: uuid.UUID | None = None,
        channel: NotificationChannel | None = None,
        status: NotificationStatus | None = None,
        since: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[Notification], int]:
        conditions: list[sa.ColumnElement[bool]] = []
        if user_id is not None:
            conditions.append(Notification.user_id == user_id)
        if channel is not None:
            conditions.append(Notification.channel == channel)
        if status is not None:
            conditions.append(Notification.status == status)
        if since is not None:
            conditions.append(Notification.created_at >= since)

        total = int(
            self.session.execute(
                sa.select(sa.func.count()).select_from(Notification).where(*conditions)
            ).scalar_one()
        )
        rows = (
            self.session.execute(
                sa.select(Notification)
                .where(*conditions)
                .options(selectinload(Notification.job))
                .order_by(Notification.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            .scalars()
            .all()
        )
        return rows, total

    def list_retryable(self, *, max_attempts: int, limit: int = 100) -> Sequence[Notification]:
        """Failed deliveries still worth another attempt.

        Retries are driven from this table rather than from Celery's own retry
        mechanism so that a broker restart does not silently drop them.
        """
        stmt = (
            sa.select(Notification)
            .where(
                Notification.status == NotificationStatus.FAILED,
                Notification.attempts < max_attempts,
            )
            .options(selectinload(Notification.job), selectinload(Notification.user))
            .order_by(Notification.created_at.asc())
            .limit(limit)
        )
        return self.session.execute(stmt).scalars().all()

    def count_by_status(self, since: datetime | None = None) -> dict[str, int]:
        stmt = sa.select(Notification.status, sa.func.count()).group_by(
            Notification.status
        )
        if since is not None:
            stmt = stmt.where(Notification.created_at >= since)
        return {str(status): int(count) for status, count in self.session.execute(stmt)}
