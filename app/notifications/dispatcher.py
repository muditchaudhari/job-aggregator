"""Notification dispatch.

Owns the two things that must not live in a channel:

* **Idempotency.** A ``notifications`` row is created *before* the send, and a
  unique constraint on ``(job_id, user_id, channel)`` makes a duplicate ping
  impossible even if a Celery task is retried or two workers race.
* **Outcome recording.** Every attempt lands in the table with its status and
  error, so retries are driven from data rather than from a queue that may
  already have been drained.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import NotificationError
from app.core.logging import get_logger
from app.core.metrics import notifications_sent
from app.models.enums import NotificationChannel, NotificationStatus
from app.models.notification import Notification
from app.notifications.base import NotificationPayload, NotificationSender
from app.notifications.channels import build_sender
from app.repositories.notification import NotificationRepository
from app.utils.time import utcnow

if TYPE_CHECKING:
    from app.matcher.base import MatchResult
    from app.models.job import Job
    from app.models.user import User

logger = get_logger(__name__)


class NotificationDispatcher:
    def __init__(
        self,
        session: Session,
        *,
        senders: dict[NotificationChannel, NotificationSender] | None = None,
    ) -> None:
        self.session = session
        self.settings = get_settings()
        self.repository = NotificationRepository(session)
        self._senders = senders or {}

    def enabled_channels(self) -> list[NotificationChannel]:
        """Channels that are both requested and actually configured.

        A channel named in ``NOTIFY_CHANNELS`` without credentials is a
        misconfiguration worth a warning, not a crash — the other channels
        should still deliver.
        """
        channels: list[NotificationChannel] = []
        for name in self.settings.enabled_notification_channels:
            try:
                channel = NotificationChannel(name.strip().lower())
            except ValueError:
                logger.warning("notify.unknown_channel", channel=name)
                continue
            if not self._sender(channel).is_configured():
                logger.warning("notify.channel_not_configured", channel=channel)
                continue
            channels.append(channel)
        return channels

    def notify(self, user: User, job: Job, match: MatchResult) -> list[Notification]:
        """Deliver one job to one user on every enabled channel."""
        payload = NotificationPayload.build(job, match)
        records: list[Notification] = []

        for channel in self.enabled_channels():
            record = self._claim(job, user, channel, match.score)
            if record is None:
                logger.debug(
                    "notify.already_sent", job=job.title, channel=channel
                )
                continue
            self._deliver(record, user, payload, channel)
            records.append(record)

        return records

    def retry_failed(self, *, max_attempts: int = 3, limit: int = 100) -> int:
        """Re-attempt deliveries that failed earlier. Returns successes."""
        pending = self.repository.list_retryable(max_attempts=max_attempts, limit=limit)
        succeeded = 0

        for record in pending:
            from app.matcher.base import MatchResult

            payload = NotificationPayload.build(
                record.job,
                MatchResult(
                    score=record.match_score or 0.0,
                    reasoning="(re-sent after an earlier delivery failure)",
                ),
            )
            self._deliver(record, record.user, payload, record.channel)
            if record.status is NotificationStatus.SENT:
                succeeded += 1

        return succeeded

    # -- Internals ---------------------------------------------------------

    def _claim(
        self,
        job: Job,
        user: User,
        channel: NotificationChannel,
        score: float,
    ) -> Notification | None:
        """Reserve the right to notify, or return ``None`` if already claimed.

        The pre-check is an optimisation; the constraint is the guarantee. A
        concurrent worker can pass the check and still lose the insert, which
        is exactly the race the ``IntegrityError`` branch handles.
        """
        if self.repository.already_notified(job.id, user.id, channel):
            return None

        record = Notification(
            job_id=job.id,
            user_id=user.id,
            channel=channel,
            status=NotificationStatus.PENDING,
            match_score=score,
        )
        try:
            self.session.add(record)
            self.session.flush()
        except IntegrityError:
            self.session.rollback()
            logger.debug("notify.race_lost", job=str(job.id), channel=channel)
            return None
        return record

    def _deliver(
        self,
        record: Notification,
        user: User,
        payload: NotificationPayload,
        channel: NotificationChannel,
    ) -> None:
        record.attempts += 1
        try:
            self._sender(channel).send(user, payload)
        except NotificationError as exc:
            record.status = NotificationStatus.FAILED
            record.error = str(exc)[:1000]
            notifications_sent.labels(channel, "failed").inc()
            logger.error(
                "notify.failed",
                channel=channel,
                job=payload.job_title,
                attempts=record.attempts,
                error=str(exc),
            )
        else:
            record.status = NotificationStatus.SENT
            record.sent_at = utcnow()
            record.error = None
            notifications_sent.labels(channel, "sent").inc()
            logger.info(
                "notify.sent",
                channel=channel,
                job=payload.job_title,
                company=payload.company_name,
                score=payload.match_score,
            )
        self.session.flush()

    def _sender(self, channel: NotificationChannel) -> NotificationSender:
        if channel not in self._senders:
            self._senders[channel] = build_sender(channel)
        return self._senders[channel]
