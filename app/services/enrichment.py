"""Detail enrichment.

Listings are thin by design — a title, a location, sometimes a date. The fact
that decides whether a job is worth your time ("12+ years of software
engineering experience") is on the posting's own page, so filtering on
experience is impossible without going and getting it.

Two things keep this affordable:

* **Only shortlisted jobs.** A posting already rejected on title or location
  gains nothing from its description, so it is never fetched.
* **A hard cap per run.** One request per job means a 787-posting board could
  otherwise turn a 6-second scan into a 13-minute one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.matcher.rule_matcher import RuleMatcher
from app.models.enums import SeniorityLevel
from app.models.job import Job
from app.normalization.dates import parse_posted_datetime
from app.normalization.fields import detect_skills, parse_seniority
from app.scrapers.base import BaseScraper
from app.utils.text import clean_text

if TYPE_CHECKING:
    from app.models.user import UserProfile

logger = get_logger(__name__)


@dataclass(slots=True)
class EnrichmentReport:
    considered: int = 0
    fetched: int = 0
    enriched: int = 0
    reclassified: int = 0


class JobEnricher:
    def __init__(self, session: Session, scraper: BaseScraper, *, limit: int = 40) -> None:
        self.session = session
        self.scraper = scraper
        self.limit = limit
        self._matcher = RuleMatcher()

    def enrich(self, jobs: list[Job], profiles: list[UserProfile]) -> EnrichmentReport:
        report = EnrichmentReport()
        candidates = self._shortlist(jobs, profiles)
        report.considered = len(candidates)

        for job in candidates[: self.limit]:
            detail = self.scraper.fetch_detail(job.url)
            report.fetched += 1
            if detail is None or not detail.is_useful():
                continue

            before = job.seniority
            self._apply(job, detail)
            report.enriched += 1
            if job.seniority is not before:
                report.reclassified += 1
                logger.debug(
                    "enrich.reclassified",
                    title=job.title,
                    was=str(before),
                    now=str(job.seniority),
                )

        self.session.flush()
        logger.info(
            "enrich.done",
            considered=report.considered,
            fetched=report.fetched,
            enriched=report.enriched,
            reclassified=report.reclassified,
        )
        return report

    def _shortlist(self, jobs: list[Job], profiles: list[UserProfile]) -> list[Job]:
        """Jobs worth paying a request for.

        Anything already vetoed on title or location stays vetoed whatever the
        description says, so fetching it would be pure cost. Jobs that already
        carry a description are skipped too — the listing gave us one.

        Ordered by rule score so that, when the cap bites, the requests are
        spent on the most promising postings rather than on whatever the board
        happened to list first.
        """
        scored: list[tuple[float, Job]] = []
        for job in jobs:
            if job.description or job.requirements:
                continue
            best = 0.0
            for profile in profiles:
                result = self._matcher.score(job, profile)
                if result.vetoed:
                    continue
                best = max(best, result.score)
            if best > 0:
                scored.append((best, job))

        scored.sort(key=lambda pair: -pair[0])
        return [job for _, job in scored]

    @staticmethod
    def _apply(job: Job, detail: object) -> None:
        from app.scrapers.base import JobDetail

        assert isinstance(detail, JobDetail)

        job.description = clean_text(detail.description) or job.description
        job.requirements = clean_text(detail.requirements) or job.requirements
        job.detected_skills = detect_skills(
            job.title, job.description, job.requirements
        )

        # The whole point: re-read seniority now that the requirements are
        # visible. Only overwrite when the title was silent — an explicit
        # "Senior" in the title outranks whatever the body implies.
        if job.seniority in (SeniorityLevel.UNKNOWN, None):
            revised = parse_seniority(
                job.title, f"{job.requirements or ''}\n{job.description or ''}"
            )
            if revised is not SeniorityLevel.UNKNOWN:
                job.seniority = revised

        if detail.posted_at and job.posted_at is None:
            job.posted_at = parse_posted_datetime(detail.posted_at)
