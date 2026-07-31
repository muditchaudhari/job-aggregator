"""Health and metrics."""

from __future__ import annotations

from datetime import timedelta

import redis
from fastapi import APIRouter, Query, Response, status

from app.api.deps import DbSession
from app.core.config import get_settings
from app.core.metrics import render_metrics
from app.database.session import check_database
from app.llm.budget import get_budget_tracker
from app.repositories.company import CompanyRepository
from app.repositories.job import JobRepository
from app.repositories.notification import NotificationRepository
from app.repositories.scrape_run import ScrapeRunRepository
from app.utils.time import utcnow

router = APIRouter(tags=["system"])


@router.get("/health")
def health(response: Response) -> dict[str, object]:
    """Liveness plus dependency readiness.

    Returns 503 when a dependency is down so an orchestrator can act on it,
    but still returns a *body* describing which one — a bare status code sends
    whoever is on call to the wrong place.
    """
    settings = get_settings()
    database_ok = check_database()
    redis_ok = _check_redis(settings.redis_url)

    healthy = database_ok and redis_ok
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ok" if healthy else "degraded",
        "environment": str(settings.environment),
        "checks": {
            "database": "ok" if database_ok else "unreachable",
            "redis": "ok" if redis_ok else "unreachable",
        },
        "timestamp": utcnow().isoformat(),
    }


@router.get("/metrics")
def metrics() -> Response:
    """Prometheus exposition."""
    return Response(
        content=render_metrics(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@router.get("/metrics/summary")
def metrics_summary(
    session: DbSession, hours: int = Query(default=24, ge=1, le=8760)
) -> dict[str, object]:
    """Human-readable aggregates.

    Complements ``/metrics``: Prometheus counters are fleet-wide and reset with
    the process, whereas these come from ``scrape_runs`` and survive restarts.
    """
    runs = ScrapeRunRepository(session).summary(window_hours=hours)
    companies = CompanyRepository(session)
    jobs = JobRepository(session)
    notifications = NotificationRepository(session)

    since = utcnow() - timedelta(hours=hours)
    delivery = notifications.count_by_status(since=since)
    delivered = delivery.get("sent", 0)
    attempted = sum(delivery.values())

    budget = get_budget_tracker().status()

    return {
        **runs,
        "companies_active": companies.count_active(),
        "companies_total": companies.count(),
        "jobs_total": jobs.count(),
        "jobs_new_in_window": jobs.count_since(since),
        "notifications": {
            **delivery,
            "success_rate": round(delivered / attempted, 4) if attempted else None,
        },
        "llm_budget": {
            "spent_usd": budget.spent_usd,
            "limit_usd": budget.limit_usd,
            "remaining_usd": budget.remaining_usd,
            "calls_today": budget.calls_today,
            "breaker_open": budget.breaker_open,
        },
    }


def _check_redis(url: str) -> bool:
    try:
        client = redis.Redis.from_url(url, socket_connect_timeout=2)
        return bool(client.ping())
    except Exception:
        return False
