"""Celery tasks.

Thin wrappers. Every task opens a session, delegates to a service, and decides
whether a failure is worth retrying — by consulting ``PlatformError.retryable``
rather than by guessing from the exception type at each call site.
"""

from __future__ import annotations

import uuid
from typing import Any

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded

from app.core.config import get_settings
from app.core.errors import PlatformError
from app.core.logging import get_logger
from app.database.session import session_scope
from app.learning.feedback import SelectorFeedback
from app.learning.selector_learner import SelectorLearner
from app.llm.client import get_llm_client
from app.notifications.dispatcher import NotificationDispatcher
from app.repositories.company import CompanyRepository
from app.repositories.job import JobRepository

# Load-bearing import, not a stray one. ``@shared_task`` binds to whichever
# Celery app is *current* in the process. A worker started with `-A` has ours;
# the API process does not, so without this it publishes to Celery's default
# app — no broker configured, `amqp://localhost`, connection refused. The
# failure is near-invisible: enqueueing is deliberately non-fatal, so
# registration returns 201 and the scan is simply never queued.
from app.scheduler.celery_app import celery_app  # noqa: F401
from app.scrapers.fetcher import Fetcher
from app.services.company import CompanyService
from app.services.scan import ScanService

logger = get_logger(__name__)


@shared_task(name="app.scheduler.tasks.scan_due_companies")
def scan_due_companies(limit: int = 500) -> dict[str, Any]:
    """Beat entry point: enqueue one task per due company.

    Fan-out rather than a single long task. One task per company means a
    failure is isolated, retries are per company, and the work spreads across
    every worker instead of pinning one for an hour.
    """
    with session_scope() as session:
        companies = CompanyRepository(session).due_for_scrape(limit=limit)
        company_ids = [str(company.id) for company in companies]

    for company_id in company_ids:
        scan_company.delay(company_id)

    logger.info("scheduler.dispatched", count=len(company_ids))
    return {"dispatched": len(company_ids), "company_ids": company_ids}


@shared_task(
    bind=True,
    name="app.scheduler.tasks.scan_company",
    max_retries=3,
    default_retry_delay=60,
)
def scan_company(
    self: Any,
    company_id: str,
    *,
    force_llm: bool = False,
    notify: bool = True,
) -> dict[str, Any]:
    try:
        with session_scope() as session, ScanService(session) as service:
            report = service.scan_company(
                uuid.UUID(company_id), force_llm=force_llm, notify=notify
            )
            return {
                "company": report.company_name,
                "status": str(report.status),
                "jobs_found": report.jobs_found,
                "jobs_new": report.jobs_new,
                "notifications": report.notifications,
                "tier": report.extraction_tier,
                "llm_cost_usd": report.llm_cost_usd,
                "duration_ms": report.duration_ms,
            }
    except SoftTimeLimitExceeded:
        # Deliberately not retried: a scan that exceeded ten minutes will
        # exceed it again, and retrying holds a worker hostage.
        logger.error("task.scan_timeout", company_id=company_id)
        raise
    except PlatformError as exc:
        if exc.retryable:
            raise self.retry(exc=exc) from exc
        logger.error("task.scan_permanent_failure", company_id=company_id, error=str(exc))
        return {"company_id": company_id, "status": "failed", "error": str(exc)}


@shared_task(name="app.scheduler.tasks.detect_company")
def detect_company(company_id: str) -> dict[str, Any]:
    """Identify a newly registered company's ATS, then scan it."""
    with session_scope() as session:
        company = CompanyService(session).detect_and_persist(uuid.UUID(company_id))
        result = {
            "company": company.name,
            "ats_type": str(company.ats_type),
            "strategy": str(company.scraping_strategy),
        }

    scan_company.delay(company_id)
    return result


@shared_task(name="app.scheduler.tasks.retry_notifications")
def retry_notifications(max_attempts: int = 3, limit: int = 100) -> dict[str, int]:
    with session_scope() as session:
        succeeded = NotificationDispatcher(session).retry_failed(
            max_attempts=max_attempts, limit=limit
        )
    logger.info("task.notifications_retried", succeeded=succeeded)
    return {"succeeded": succeeded}


@shared_task(name="app.scheduler.tasks.relearn_selectors")
def relearn_selectors(limit: int = 20) -> dict[str, Any]:
    """Refresh selectors that have degraded, off the critical path.

    Capped per run. Relearning is the expensive path, and an unbounded nightly
    job is exactly the shape of thing that quietly spends a month's budget in
    one night.
    """
    settings = get_settings()
    client = get_llm_client()
    if not client.is_available:
        return {"relearned": 0, "skipped": "llm unavailable"}

    relearned: list[str] = []
    with session_scope() as session:
        feedback = SelectorFeedback(session)
        websites = feedback.degraded_websites()[:limit]
        if not websites:
            return {"relearned": 0}

        companies = CompanyRepository(session)
        learner = SelectorLearner(session, client)

        with Fetcher() as fetcher:
            for website in websites:
                company = next(
                    (
                        c
                        for c in companies.list_filtered(is_active=True, limit=1000)
                        if c.website == website
                    ),
                    None,
                )
                if company is None:
                    continue
                try:
                    fetched = fetcher.fetch(company.career_url)
                    result = learner.learn(
                        website=website,
                        html=fetched.text,
                        url=fetched.final_url or fetched.url,
                    )
                except PlatformError as exc:
                    logger.warning("task.relearn_failed", website=website, error=str(exc))
                    continue
                if result.persisted:
                    relearned.append(website)

    logger.info(
        "task.relearned",
        count=len(relearned),
        threshold=settings.extraction_min_confidence,
    )
    return {"relearned": len(relearned), "websites": relearned}


@shared_task(name="app.scheduler.tasks.reap_stale_jobs")
def reap_stale_jobs(older_than_days: int = 7) -> dict[str, int]:
    """Close out postings that have stopped appearing on their board.

    Marked inactive, never deleted — "how long was this role open?" stays
    answerable, and a board that briefly 500s does not erase its history.
    """
    total = 0
    with session_scope() as session:
        jobs = JobRepository(session)
        for company in CompanyRepository(session).list_filtered(is_active=True, limit=5000):
            total += jobs.deactivate_stale(company.id, older_than_days=older_than_days)
    logger.info("task.stale_jobs_reaped", count=total)
    return {"deactivated": total}
