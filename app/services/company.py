"""Company registration.

Registration is deliberately cheap: canonicalise the URL, check for a
duplicate, insert, and mark the company due immediately. Detection needs a
network round trip (sometimes a browser), so it runs as a background task
rather than blocking the ``POST /companies`` response.
"""

from __future__ import annotations

import uuid
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import DuplicateError, NotFoundError
from app.core.logging import get_logger
from app.models.company import Company
from app.models.enums import ATSType, ScrapeFrequency, ScrapingStrategy
from app.repositories.company import CompanyRepository
from app.scrapers.detection import detect
from app.scrapers.fetcher import Fetcher
from app.utils.text import clean_text
from app.utils.time import utcnow
from app.utils.urls import canonicalize_url, registrable_domain

logger = get_logger(__name__)


class CompanyService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = get_settings()
        self.companies = CompanyRepository(session)

    def register(
        self,
        *,
        career_url: str,
        name: str | None = None,
        scrape_frequency: ScrapeFrequency = ScrapeFrequency.DAILY,
        scrape_interval_minutes: int | None = None,
        ats_type: ATSType | None = None,
    ) -> Company:
        url = canonicalize_url(career_url)
        if self.companies.get_by_url(url):
            raise DuplicateError("career URL is already registered", career_url=url)

        interval = scrape_interval_minutes or scrape_frequency.minutes or (
            self.settings.scheduler_default_interval_minutes
        )

        company = Company(
            name=clean_text(name) or self._infer_name(url),
            career_url=url,
            website=registrable_domain(url),
            ats_type=ats_type or ATSType.UNKNOWN,
            scraping_strategy=ScrapingStrategy.AUTO,
            scrape_frequency=scrape_frequency,
            scrape_interval_minutes=interval,
            # Due immediately: a user who just added a company expects results
            # on the next tick, not after a full interval of silence.
            next_scrape_at=utcnow(),
        )
        self.companies.add(company)
        logger.info("company.registered", name=company.name, url=url)
        return company

    def detect_and_persist(self, company_id: uuid.UUID) -> Company:
        """Identify the platform and cache the answer on the company row."""
        company = self.companies.get(company_id)
        if company is None:
            raise NotFoundError("company not found", company_id=str(company_id))

        with Fetcher() as fetcher:
            result = detect(company.career_url, fetcher)

        company.ats_type = result.ats_type
        company.scraping_strategy = result.strategy
        if result.board_token:
            company.board_token = result.board_token
        if result.api_endpoint:
            company.api_endpoint = result.api_endpoint

        self.session.flush()
        logger.info(
            "company.detected",
            name=company.name,
            ats=result.ats_type,
            strategy=result.strategy,
            confidence=result.confidence,
            evidence=result.evidence,
        )
        return company

    def deactivate(self, company_id: uuid.UUID) -> Company:
        company = self.companies.get(company_id)
        if company is None:
            raise NotFoundError("company not found", company_id=str(company_id))
        return self.companies.deactivate(company, "deactivated by user")

    @staticmethod
    def _infer_name(url: str) -> str:
        """Derive a display name when the caller did not supply one.

        For ATS-hosted boards the useful name is in the path
        (``boards.greenhouse.io/acme`` → "Acme"), not the host, which would
        otherwise name every board "Greenhouse".
        """
        parsed = urlparse(url)
        host = (parsed.hostname or "").removeprefix("www.")
        segments = [segment for segment in parsed.path.split("/") if segment]

        ats_hosts = ("greenhouse.io", "lever.co", "ashbyhq.com", "smartrecruiters.com")
        if any(marker in host for marker in ats_hosts) and segments:
            return segments[0].replace("-", " ").replace("_", " ").title()

        label = host.split(".")[0] if host else "Unknown"
        return label.replace("-", " ").title() or "Unknown"
