"""Celery application and Beat schedule.

Three queues so the process types can be scaled and isolated independently
(AD-1). Scraping is slow, memory-hungry, and occasionally crashes a browser;
notification delivery is fast and must not be stuck behind it.
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_process_init, worker_process_shutdown

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger

logger = get_logger(__name__)
settings = get_settings()

celery_app = Celery(
    "job_aggregator",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.scheduler.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    # A scrape that has run for 10 minutes is stuck, not slow. The soft limit
    # raises inside the task so it can record a failed run before the hard
    # limit kills the worker outright.
    task_soft_time_limit=600,
    task_time_limit=660,
    task_acks_late=True,
    # Without this, a prefetching worker grabs a batch of companies and then
    # sits on them while one slow render blocks the rest.
    worker_prefetch_multiplier=1,
    # Chromium leaks across long-lived processes; recycling bounds it.
    worker_max_tasks_per_child=50,
    result_expires=86_400,
    task_routes={
        "app.scheduler.tasks.scan_company": {"queue": "scrape"},
        "app.scheduler.tasks.scan_due_companies": {"queue": "scrape"},
        "app.scheduler.tasks.detect_company": {"queue": "scrape"},
        "app.scheduler.tasks.relearn_selectors": {"queue": "scrape"},
        "app.scheduler.tasks.retry_notifications": {"queue": "notify"},
        "app.scheduler.tasks.reap_stale_jobs": {"queue": "maintenance"},
    },
    beat_schedule={
        # The scheduler ticks frequently and each tick enqueues only the
        # companies whose own interval has elapsed. Per-company cadence is
        # therefore data, not a Beat entry — adding a company does not require
        # touching this config or restarting Beat.
        "dispatch-due-companies": {
            "task": "app.scheduler.tasks.scan_due_companies",
            "schedule": crontab(minute=f"*/{settings.scheduler_tick_minutes}"),
        },
        "retry-failed-notifications": {
            "task": "app.scheduler.tasks.retry_notifications",
            "schedule": crontab(minute="*/15"),
        },
        # Proactive relearning runs at night, off the critical path of any
        # user-facing scan (see learning/feedback.py).
        "relearn-degraded-selectors": {
            "task": "app.scheduler.tasks.relearn_selectors",
            "schedule": crontab(hour="3", minute="0"),
        },
        "reap-stale-jobs": {
            "task": "app.scheduler.tasks.reap_stale_jobs",
            "schedule": crontab(hour="4", minute="0"),
        },
    },
)


@worker_process_init.connect
def _configure_worker(**_: object) -> None:
    configure_logging()
    logger.info("worker.started")


@worker_process_shutdown.connect
def _shutdown_worker(**_: object) -> None:
    """Close the process's Playwright browser.

    Celery's warm shutdown does not run module finalisers, so without this an
    orphaned Chromium survives the worker and holds its memory.
    """
    from app.scrapers.browser import shutdown_browser

    shutdown_browser()
    logger.info("worker.stopped")
