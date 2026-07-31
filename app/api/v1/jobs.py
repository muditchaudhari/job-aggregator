from __future__ import annotations

import uuid
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import DbSession, OptionalProfile, Pagination
from app.models.enums import RemoteType
from app.repositories.company import CompanyRepository
from app.repositories.job import JobRepository
from app.repositories.match import JobMatchRepository
from app.schemas.common import Page
from app.schemas.job import JobDetail, JobRead
from app.utils.time import utcnow

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("", response_model=Page[JobRead])
def list_jobs(
    session: DbSession,
    page: Pagination,
    company_id: uuid.UUID | None = Query(default=None),
    location: str | None = Query(default=None, max_length=255),
    remote_type: RemoteType | None = Query(default=None),
    title: str | None = Query(default=None, max_length=255),
    is_active: bool | None = Query(default=True),
) -> Page[JobRead]:
    jobs, total = JobRepository(session).list_filtered(
        company_id=company_id,
        location=location,
        remote_type=remote_type,
        title_contains=title,
        is_active=is_active,
        limit=page.limit,
        offset=page.offset,
    )
    return Page[JobRead](
        items=[JobRead.model_validate(job) for job in jobs],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/new", response_model=Page[JobDetail])
def list_new_jobs(
    session: DbSession,
    page: Pagination,
    profile: OptionalProfile,
    hours: int = Query(default=24, ge=1, le=720),
    min_score: float | None = Query(default=None, ge=0.0, le=1.0),
) -> Page[JobDetail]:
    """Postings first seen within a window, annotated with match scores.

    "New" means *first seen by us*, not the posting's own date. A board that
    publishes without dates, or backdates its postings, would otherwise make
    this endpoint return nothing — and first-seen is what the notification
    logic acts on, so the two stay consistent.
    """
    repository = JobRepository(session)
    since = utcnow() - timedelta(hours=hours)
    jobs, total = repository.list_new_since(since, limit=page.limit, offset=page.offset)

    scores: dict[uuid.UUID, float] = {}
    if profile is not None:
        scores = JobMatchRepository(session).scores_for_jobs(
            [job.id for job in jobs], profile.id
        )

    items: list[JobDetail] = []
    for job in jobs:
        score = scores.get(job.id)
        if min_score is not None and (score is None or score < min_score):
            continue
        detail = JobDetail.model_validate(job)
        detail.company_name = job.company.name if job.company else None
        detail.match_score = score
        items.append(detail)

    return Page[JobDetail](items=items, total=total, limit=page.limit, offset=page.offset)


@router.get("/{job_id}", response_model=JobDetail)
def get_job(job_id: uuid.UUID, session: DbSession, profile: OptionalProfile) -> JobDetail:
    job = JobRepository(session).get(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")

    detail = JobDetail.model_validate(job)
    detail.company_name = job.company.name if job.company else None

    if profile is not None:
        match = JobMatchRepository(session).get_for_job(job.id, profile.id)
        if match is not None:
            detail.match_score = match.score
            detail.match_reasoning = match.reasoning
            detail.matched_skills = list(match.matched_skills or [])
            detail.missing_skills = list(match.missing_skills or [])

    return detail


@router.get("/by-company/{company_id}", response_model=Page[JobRead])
def list_company_jobs(
    company_id: uuid.UUID, session: DbSession, page: Pagination
) -> Page[JobRead]:
    if CompanyRepository(session).get(company_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="company not found")

    jobs, total = JobRepository(session).list_filtered(
        company_id=company_id, limit=page.limit, offset=page.offset
    )
    return Page[JobRead](
        items=[JobRead.model_validate(job) for job in jobs],
        total=total,
        limit=page.limit,
        offset=page.offset,
    )
