"""Notification rendering, dispatch, idempotency, and failure recording."""

from __future__ import annotations

import uuid
from typing import Any, cast

from sqlalchemy.orm import Session

from app.matcher.base import MatchResult
from app.models.enums import NotificationChannel, NotificationStatus, RemoteType
from app.models.job import Job
from app.notifications.base import NotificationPayload
from app.notifications.dispatcher import NotificationDispatcher
from app.notifications.templates import (
    render_email_html,
    render_email_subject,
    render_email_text,
    render_slack_blocks,
    render_telegram,
)
from tests.fixtures.fakes import RecordingSender


def make_payload(**overrides: Any) -> NotificationPayload:
    defaults: dict[str, Any] = {
        "job_title": "Senior Backend Engineer",
        "company_name": "Acme Corp",
        "location": "Bengaluru, India",
        "url": "https://boards.greenhouse.io/acme/jobs/1",
        "match_score": 0.87,
        "reasoning": "Title matches a preferred role; mentions Python, AWS.",
        "matched_skills": ["Python", "AWS"],
        "missing_skills": ["Kubernetes"],
        "posted_date": "2026-07-28",
        "salary": "₹25,00,000 - ₹35,00,000",
        "remote_type": "hybrid",
        "employment_type": "full_time",
    }
    defaults.update(overrides)
    return NotificationPayload(**defaults)


class TestEmailTemplate:
    def test_subject_carries_score_and_company(self) -> None:
        subject = render_email_subject(make_payload())
        assert "87%" in subject
        assert "Acme Corp" in subject

    def test_html_contains_every_required_element(self) -> None:
        """The brief specifies exactly what an email must show."""
        html = render_email_html(make_payload())
        for expected in (
            "Acme Corp",
            "Senior Backend Engineer",
            "Bengaluru, India",
            "87% match",
            "Why this matched",
            "2026-07-28",
            "https://boards.greenhouse.io/acme/jobs/1",
        ):
            assert expected in html

    def test_html_escapes_injected_markup(self) -> None:
        html = render_email_html(make_payload(job_title="<script>alert(1)</script>"))
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_plain_text_alternative_is_produced(self) -> None:
        """A multipart email without a text part is a spam signal."""
        text = render_email_text(make_payload())
        assert "Apply:" in text
        assert "87%" in text
        assert "Senior Backend Engineer" in text
        # Genuinely plain: no markup leaked in from the HTML renderer.
        assert "<" not in text and ">" not in text

    def test_optional_fields_are_omitted_cleanly(self) -> None:
        html = render_email_html(make_payload(salary=None, posted_date=None, reasoning=None))
        assert "Salary" not in html
        assert "Why this matched" not in html


class TestOtherChannels:
    def test_slack_payload_has_a_text_fallback(self) -> None:
        """Without ``text`` the desktop notification is blank."""
        payload = render_slack_blocks(make_payload())
        assert payload["text"]
        assert any(block["type"] == "header" for block in payload["blocks"])

    def test_slack_button_links_to_the_posting(self) -> None:
        blocks = render_slack_blocks(make_payload())["blocks"]
        actions = next(block for block in blocks if block["type"] == "actions")
        assert actions["elements"][0]["url"].endswith("/jobs/1")

    def test_telegram_uses_supported_tags_only(self) -> None:
        text = render_telegram(make_payload())
        assert "<b>" in text and "<a href=" in text
        assert "<div" not in text and "<span" not in text


class TestDispatcher:
    def _job(self, db_session: Session, company: Any) -> Job:
        job = Job(
            company_id=company.id,
            title="Senior Backend Engineer",
            url="https://x.com/jobs/1",
            location_raw="Bengaluru, India",
            remote_type=RemoteType.HYBRID,
            content_hash=uuid.uuid4().hex,
        )
        db_session.add(job)
        db_session.flush()
        return job

    def _dispatcher(
        self, db_session: Session, sender: RecordingSender
    ) -> NotificationDispatcher:
        return NotificationDispatcher(
            db_session, senders={NotificationChannel.EMAIL: cast(Any, sender)}
        )

    def test_sends_and_records(
        self, db_session: Session, company: Any, user: Any
    ) -> None:
        sender = RecordingSender(NotificationChannel.EMAIL)
        job = self._job(db_session, company)

        records = self._dispatcher(db_session, sender).notify(
            user, job, MatchResult(score=0.9, reasoning="strong fit")
        )

        assert len(sender.sent) == 1
        assert records[0].status is NotificationStatus.SENT
        assert records[0].sent_at is not None

    def test_second_notification_for_the_same_job_is_suppressed(
        self, db_session: Session, company: Any, user: Any
    ) -> None:
        """The guarantee that stops duplicate pings on a retried task."""
        sender = RecordingSender(NotificationChannel.EMAIL)
        job = self._job(db_session, company)
        dispatcher = self._dispatcher(db_session, sender)
        match = MatchResult(score=0.9)

        dispatcher.notify(user, job, match)
        dispatcher.notify(user, job, match)

        assert len(sender.sent) == 1

    def test_failure_is_recorded_with_the_error(
        self, db_session: Session, company: Any, user: Any
    ) -> None:
        sender = RecordingSender(NotificationChannel.EMAIL, fail=True)
        job = self._job(db_session, company)

        records = self._dispatcher(db_session, sender).notify(
            user, job, MatchResult(score=0.9)
        )

        assert records[0].status is NotificationStatus.FAILED
        assert records[0].error
        assert records[0].attempts == 1

    def test_failed_delivery_can_be_retried(
        self, db_session: Session, company: Any, user: Any
    ) -> None:
        failing = RecordingSender(NotificationChannel.EMAIL, fail=True)
        job = self._job(db_session, company)
        self._dispatcher(db_session, failing).notify(user, job, MatchResult(score=0.9))
        db_session.flush()

        working = RecordingSender(NotificationChannel.EMAIL)
        succeeded = self._dispatcher(db_session, working).retry_failed(max_attempts=3)

        assert succeeded == 1
        assert len(working.sent) == 1
