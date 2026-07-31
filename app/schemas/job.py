from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import Field

from app.models.enums import (
    EmploymentType,
    ExtractionTier,
    RemoteType,
    SalaryPeriod,
    SeniorityLevel,
)
from app.schemas.common import ORMModel


class JobRead(ORMModel):
    id: uuid.UUID
    company_id: uuid.UUID
    external_job_id: str | None
    title: str
    url: str
    location_raw: str | None
    location_city: str | None
    location_region: str | None
    location_country: str | None
    remote_type: RemoteType
    employment_type: EmploymentType
    seniority: SeniorityLevel
    salary_min: int | None
    salary_max: int | None
    salary_currency: str | None
    salary_period: SalaryPeriod
    salary_raw: str | None
    posted_date: date | None
    detected_skills: list[str]
    extraction_tier: ExtractionTier
    first_seen_at: datetime
    is_active: bool


class JobDetail(JobRead):
    description: str | None
    requirements: str | None
    company_name: str | None = None
    #: Populated for the requesting profile only, so a job's relevance is
    #: reported from the caller's point of view rather than someone else's.
    match_score: float | None = None
    match_reasoning: str | None = None
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
