"""Repositories: deduplication, scheduling, and notification idempotency."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models.enums import NotificationChannel, NotificationStatus
from app.models.job import Job
from app.repositories.company import CompanyRepository
from app.repositories.job import JobRepository
from app.repositories.notification import NotificationRepository
from app.utils.time import utcnow


def make_job(company_id: uuid.UUID, title: str, content_hash: str) -> Job:
    return Job(
        company_id=company_id,
        title=title,
        url=f"https://x.com/jobs/{content_hash[:8]}",
        content_hash=content_hash,
    )


class TestDeduplication:
    def test_first_insert_returns_everything(
        self, db_session: Session, company: Any
    ) -> None:
        jobs = [make_job(company.id, f"Role {i}", f"hash{i}") for i in range(3)]
        assert len(JobRepository(db_session).insert_new(jobs)) == 3

    def test_second_insert_returns_only_the_new(
        self, db_session: Session, company: Any
    ) -> None:
        repository = JobRepository(db_session)
        repository.insert_new([make_job(company.id, f"Role {i}", f"hash{i}") for i in range(3)])
        db_session.commit()

        second_batch = [
            make_job(company.id, f"Role {i}", f"hash{i}") for i in range(5)
        ]
        fresh = repository.insert_new(second_batch)

        assert len(fresh) == 2
        assert {job.content_hash for job in fresh} == {"hash3", "hash4"}

    def test_duplicates_within_one_batch_are_collapsed(
        self, db_session: Session, company: Any
    ) -> None:
        """A posting listed under two departments is common, not an error."""
        batch = [
            make_job(company.id, "Engineer", "same"),
            make_job(company.id, "Engineer", "same"),
        ]
        assert len(JobRepository(db_session).insert_new(batch)) == 1

    def test_same_hash_under_different_companies_coexists(
        self, db_session: Session, company: Any, generic_company: Any
    ) -> None:
        repository = JobRepository(db_session)
        repository.insert_new([make_job(company.id, "Engineer", "shared")])
        fresh = repository.insert_new([make_job(generic_company.id, "Engineer", "shared")])
        assert len(fresh) == 1

    def test_empty_batch_is_a_no_op(self, db_session: Session) -> None:
        assert JobRepository(db_session).insert_new([]) == []


class TestJobLifecycle:
    def test_touch_seen_updates_last_seen(
        self, db_session: Session, company: Any
    ) -> None:
        repository = JobRepository(db_session)
        repository.insert_new([make_job(company.id, "Engineer", "h1")])
        db_session.commit()

        job = repository.get_by_hash(company.id, "h1")
        assert job is not None
        original = job.last_seen_at

        repository.touch_seen(company.id, ["h1"])
        db_session.commit()
        db_session.refresh(job)
        assert job.last_seen_at >= original

    def test_stale_jobs_are_deactivated_not_deleted(
        self, db_session: Session, company: Any
    ) -> None:
        repository = JobRepository(db_session)
        repository.insert_new([make_job(company.id, "Engineer", "h1")])
        db_session.commit()

        job = repository.get_by_hash(company.id, "h1")
        assert job is not None
        job.last_seen_at = utcnow() - timedelta(days=30)
        db_session.commit()

        assert repository.deactivate_stale(company.id, older_than_days=7) == 1
        db_session.commit()
        db_session.refresh(job)
        assert job.is_active is False
        # History is retained.
        assert repository.get_by_hash(company.id, "h1") is not None


class TestScheduling:
    def test_never_scraped_company_is_due(
        self, db_session: Session, company: Any
    ) -> None:
        company.next_scrape_at = None
        db_session.commit()
        assert company.id in {c.id for c in CompanyRepository(db_session).due_for_scrape()}

    def test_future_company_is_not_due(self, db_session: Session, company: Any) -> None:
        company.next_scrape_at = utcnow() + timedelta(hours=5)
        db_session.commit()
        assert company.id not in {
            c.id for c in CompanyRepository(db_session).due_for_scrape()
        }

    def test_inactive_company_is_never_due(
        self, db_session: Session, company: Any
    ) -> None:
        company.is_active = False
        company.next_scrape_at = None
        db_session.commit()
        assert CompanyRepository(db_session).due_for_scrape() == []

    def test_success_schedules_at_the_normal_interval(self, company: Any) -> None:
        company.consecutive_failures = 4
        company.schedule_next(failed=False)
        assert company.consecutive_failures == 0
        delta = company.next_scrape_at - company.last_scraped_at
        assert delta == timedelta(minutes=company.scrape_interval_minutes)

    def test_failure_backs_off_exponentially(self, company: Any) -> None:
        company.scrape_interval_minutes = 60
        company.schedule_next(failed=True)
        first = company.next_scrape_at - company.last_scraped_at
        company.schedule_next(failed=True)
        second = company.next_scrape_at - company.last_scraped_at
        assert second > first

    def test_backoff_is_capped_at_a_week(self, company: Any) -> None:
        company.consecutive_failures = 30
        company.schedule_next(failed=True)
        delta = company.next_scrape_at - company.last_scraped_at
        assert delta <= timedelta(days=7)

    def test_deactivate_is_a_soft_delete(
        self, db_session: Session, company: Any
    ) -> None:
        CompanyRepository(db_session).deactivate(company, "test")
        db_session.commit()
        assert company.is_active is False
        assert CompanyRepository(db_session).get(company.id) is not None


class TestNotificationIdempotency:
    def test_already_notified_detects_a_prior_send(
        self, db_session: Session, company: Any, user: Any
    ) -> None:
        from app.models.notification import Notification

        job = make_job(company.id, "Engineer", "h1")
        db_session.add(job)
        db_session.flush()

        repository = NotificationRepository(db_session)
        assert not repository.already_notified(job.id, user.id, NotificationChannel.EMAIL)

        db_session.add(
            Notification(
                job_id=job.id,
                user_id=user.id,
                channel=NotificationChannel.EMAIL,
                status=NotificationStatus.SENT,
            )
        )
        db_session.flush()

        assert repository.already_notified(job.id, user.id, NotificationChannel.EMAIL)

    def test_failed_delivery_does_not_block_a_retry(
        self, db_session: Session, company: Any, user: Any
    ) -> None:
        from app.models.notification import Notification

        job = make_job(company.id, "Engineer", "h1")
        db_session.add(job)
        db_session.flush()
        db_session.add(
            Notification(
                job_id=job.id,
                user_id=user.id,
                channel=NotificationChannel.EMAIL,
                status=NotificationStatus.FAILED,
                attempts=1,
            )
        )
        db_session.flush()

        repository = NotificationRepository(db_session)
        assert not repository.already_notified(job.id, user.id, NotificationChannel.EMAIL)
        assert len(repository.list_retryable(max_attempts=3)) == 1
