"""Scan triggers.

Both endpoints enqueue rather than execute. A scan involves network I/O, a
possible browser render, and sometimes a model call — seconds to minutes. Doing
that inside a request would tie up a threadpool slot and time out behind any
reverse proxy.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.api.deps import DbSession
from app.core.logging import get_logger
from app.repositories.company import CompanyRepository
from app.repositories.scrape_run import ScrapeRunRepository
from app.schemas.notification import ScanRequest, ScanResponse

logger = get_logger(__name__)
router = APIRouter(tags=["scans"])


@router.post("/scan", response_model=ScanResponse, status_code=status.HTTP_202_ACCEPTED)
def trigger_scan(payload: ScanRequest, session: DbSession) -> ScanResponse:
    """Scan companies that are due, or one specific company."""
    repository = CompanyRepository(session)

    if payload.company_id is not None:
        company = repository.get(payload.company_id)
        if company is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="company not found"
            )
        targets = [company]
    else:
        targets = list(repository.due_for_scrape(limit=payload.limit))

    return _dispatch(targets, payload, verb="scan")


@router.post("/rescan", response_model=ScanResponse, status_code=status.HTTP_202_ACCEPTED)
def trigger_rescan(payload: ScanRequest, session: DbSession) -> ScanResponse:
    """Force a scan regardless of schedule.

    Differs from ``/scan`` in ignoring ``next_scrape_at``. Combined with
    ``force_llm``, this is the "the site changed, relearn it" button.
    """
    repository = CompanyRepository(session)

    if payload.company_id is not None:
        company = repository.get(payload.company_id)
        if company is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="company not found"
            )
        targets = [company]
    else:
        targets = list(repository.list_filtered(is_active=True, limit=payload.limit))

    return _dispatch(targets, payload, verb="rescan")


@router.get("/runs")
def list_recent_runs(session: DbSession, limit: int = 50) -> list[dict[str, object]]:
    """Recent scan telemetry — the per-company detail metrics cannot carry."""
    runs = ScrapeRunRepository(session).recent(limit=min(limit, 200))
    return [
        {
            "id": str(run.id),
            "company_id": str(run.company_id),
            "status": str(run.status),
            "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "extraction_tier": str(run.extraction_tier) if run.extraction_tier else None,
            "selector_version": run.selector_version,
            "confidence": run.confidence,
            "jobs_found": run.jobs_found,
            "jobs_new": run.jobs_new,
            "duplicates": run.jobs_duplicate,
            "notifications_sent": run.notifications_sent,
            "llm_calls": run.llm_calls,
            "llm_cost_usd": run.llm_cost_usd,
            "total_ms": run.total_ms,
            "error": run.error,
        }
        for run in runs
    ]


def _dispatch(targets: list, payload: ScanRequest, *, verb: str) -> ScanResponse:
    from app.scheduler.tasks import scan_company

    company_ids = [company.id for company in targets]
    if not company_ids:
        return ScanResponse(scheduled=0, company_ids=[], message="nothing to scan")

    try:
        for company_id in company_ids:
            scan_company.delay(
                str(company_id), force_llm=payload.force_llm, notify=payload.notify
            )
    except Exception as exc:  # pragma: no cover - broker availability
        logger.error("api.scan_enqueue_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"could not reach the task queue: {exc}",
        ) from exc

    logger.info(f"api.{verb}_requested", count=len(company_ids), force_llm=payload.force_llm)
    return ScanResponse(
        scheduled=len(company_ids),
        company_ids=company_ids,
        message=f"{len(company_ids)} companies queued for {verb}",
    )
