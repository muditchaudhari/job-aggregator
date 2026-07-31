"""Notification channel contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.models.enums import NotificationChannel

if TYPE_CHECKING:
    from app.matcher.base import MatchResult
    from app.models.job import Job
    from app.models.user import User


@dataclass(slots=True)
class NotificationPayload:
    """Everything a channel needs, already resolved.

    Channels receive this rather than ORM objects so that rendering cannot
    trigger a lazy load — and, more practically, so a channel can be unit
    tested without a database.
    """

    job_title: str
    company_name: str
    location: str
    url: str
    match_score: float
    reasoning: str | None
    matched_skills: list[str]
    missing_skills: list[str]
    posted_date: str | None
    salary: str | None
    remote_type: str
    employment_type: str

    @classmethod
    def build(cls, job: Job, match: MatchResult) -> NotificationPayload:
        return cls(
            job_title=job.title,
            company_name=job.company.name if job.company else "Unknown",
            location=job.location_raw or "Not specified",
            url=job.url,
            match_score=match.score,
            reasoning=match.reasoning,
            matched_skills=match.matched_skills,
            missing_skills=match.missing_skills,
            posted_date=job.posted_date.isoformat() if job.posted_date else None,
            salary=job.salary_raw,
            remote_type=str(job.remote_type),
            employment_type=str(job.employment_type),
        )


class NotificationSender(ABC):
    channel: NotificationChannel

    @abstractmethod
    def send(self, user: User, payload: NotificationPayload) -> None:
        """Deliver, or raise ``NotificationError``.

        Success is signalled by returning normally. Channels do not record
        their own outcomes — the dispatcher owns the ``notifications`` row so
        that idempotency and retry logic live in one place.
        """

    @abstractmethod
    def is_configured(self) -> bool:
        """Whether this channel has the credentials it needs."""
