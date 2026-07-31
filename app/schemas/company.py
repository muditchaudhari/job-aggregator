from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, HttpUrl, field_validator

from app.models.enums import ATSType, ScrapeFrequency, ScrapingStrategy
from app.schemas.common import ORMModel


class CompanyCreate(BaseModel):
    career_url: HttpUrl
    name: str | None = Field(default=None, max_length=255)
    scrape_frequency: ScrapeFrequency = ScrapeFrequency.DAILY
    #: Only meaningful with ``scrape_frequency=custom``; ignored otherwise.
    scrape_interval_minutes: int | None = Field(default=None, ge=5, le=43_200)
    #: Skips detection when the caller already knows the platform.
    ats_type: ATSType | None = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str | None) -> str | None:
        return value.strip() if value else None


class CompanyUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    scrape_frequency: ScrapeFrequency | None = None
    scrape_interval_minutes: int | None = Field(default=None, ge=5, le=43_200)
    is_active: bool | None = None
    scraping_strategy: ScrapingStrategy | None = None


class CompanyRead(ORMModel):
    id: uuid.UUID
    name: str
    career_url: str
    website: str
    ats_type: ATSType
    scraping_strategy: ScrapingStrategy
    board_token: str | None
    scrape_frequency: ScrapeFrequency
    scrape_interval_minutes: int
    last_scraped_at: datetime | None
    next_scrape_at: datetime | None
    consecutive_failures: int
    last_error: str | None
    is_active: bool
    created_at: datetime


class CompanyDetail(CompanyRead):
    """Company plus a summary of its most recent run."""

    total_jobs: int = 0
    active_jobs: int = 0
    last_run_status: str | None = None
    last_run_jobs_found: int | None = None
    last_run_tier: str | None = None
    selector_confidence: float | None = None
