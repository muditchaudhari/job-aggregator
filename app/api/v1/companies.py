from __future__ import annotations

import uuid

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import DbSession, Pagination, TransactionalSession
from app.core.errors import DuplicateError, NotFoundError
from app.core.logging import get_logger
from app.models.enums import ATSType
from app.models.job import Job
from app.repositories.company import CompanyRepository
from app.repositories.scrape_run import ScrapeRunRepository
from app.repositories.selector import SelectorRepository
from app.schemas.common import MessageResponse, Page
from app.schemas.company import CompanyCreate, CompanyDetail, CompanyRead, CompanyUpdate
from app.services.company import CompanyService

logger = get_logger(__name__)
router = APIRouter(prefix="/companies", tags=["companies"])


@router.post("", response_model=CompanyRead, status_code=status.HTTP_201_CREATED)
def create_company(payload: CompanyCreate, session: TransactionalSession) -> CompanyRead:
    """Register a career page.

    Returns as soon as the row is written. ATS detection needs a network round
    trip and sometimes a browser render, so it is queued rather than made part
    of the request — the company is marked due immediately and the detection
    task scans it straight afterwards.
    """
    service = CompanyService(session)
    try:
        company = service.register(
            career_url=str(payload.career_url),
            name=payload.name,
            scrape_frequency=payload.scrape_frequency,
            scrape_interval_minutes=payload.scrape_interval_minutes,
            ats_type=payload.ats_type,
        )
    except DuplicateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    session.flush()
    _enqueue_detection(company.id)
    return CompanyRead.model_validate(company)


@router.get("", response_model=Page[CompanyRead])
def list_companies(
    session: DbSession,
    page: Pagination,
    ats_type: ATSType | None = Query(default=None),
    is_active: bool | None = Query(default=None),
) -> Page[CompanyRead]:
    repository = CompanyRepository(session)
    companies = repository.list_filtered(
        ats_type=ats_type, is_active=is_active, limit=page.limit, offset=page.offset
    )
    return Page[CompanyRead](
        items=[CompanyRead.model_validate(company) for company in companies],
        total=repository.count(),
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/{company_id}", response_model=CompanyDetail)
def get_company(company_id: uuid.UUID, session: DbSession) -> CompanyDetail:
    company = CompanyRepository(session).get(company_id)
    if company is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="company not found")

    detail = CompanyDetail.model_validate(company)

    counts = session.execute(
        sa.select(
            sa.func.count(),
            sa.func.count().filter(Job.is_active.is_(True)),
        ).where(Job.company_id == company_id)
    ).one()
    detail.total_jobs = int(counts[0])
    detail.active_jobs = int(counts[1])

    last_run = ScrapeRunRepository(session).latest_for_company(company_id)
    if last_run is not None:
        detail.last_run_status = str(last_run.status)
        detail.last_run_jobs_found = last_run.jobs_found
        detail.last_run_tier = str(last_run.extraction_tier) if last_run.extraction_tier else None

    selector = SelectorRepository(session).get_active(company.website)
    if selector is not None:
        detail.selector_confidence = selector.confidence_score

    return detail


@router.patch("/{company_id}", response_model=CompanyRead)
def update_company(
    company_id: uuid.UUID, payload: CompanyUpdate, session: TransactionalSession
) -> CompanyRead:
    repository = CompanyRepository(session)
    company = repository.get(company_id)
    if company is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="company not found")

    updates = payload.model_dump(exclude_unset=True)
    frequency = updates.get("scrape_frequency")
    if frequency is not None and updates.get("scrape_interval_minutes") is None:
        # Keep the interval consistent with a named frequency; otherwise
        # switching daily → hourly would change the label and nothing else.
        minutes = frequency.minutes
        if minutes is not None:
            updates["scrape_interval_minutes"] = minutes

    repository.update(company, **updates)
    return CompanyRead.model_validate(company)


@router.delete("/{company_id}", response_model=MessageResponse)
def delete_company(company_id: uuid.UUID, session: TransactionalSession) -> MessageResponse:
    """Soft delete.

    Hard deletion would cascade away every job and every notification ever sent
    for this company, destroying delivery history the user may still be reading.
    """
    try:
        company = CompanyService(session).deactivate(company_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return MessageResponse(
        message="company deactivated",
        detail=f"{company.name} will no longer be scanned; its jobs are retained.",
    )


def _enqueue_detection(company_id: uuid.UUID) -> None:
    """Queue detection, degrading to synchronous-later if the broker is down.

    An unreachable broker must not fail the registration — the company row is
    valid and the scheduler will pick it up on the next tick regardless.
    """
    try:
        from app.scheduler.tasks import detect_company

        detect_company.delay(str(company_id))
    except Exception as exc:  # pragma: no cover - broker availability
        logger.warning("api.detection_enqueue_failed", company_id=str(company_id), error=str(exc))
