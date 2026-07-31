"""Scan orchestration.

The pipeline from ARCHITECTURE §4, in one readable method:

    fetch → extract → normalise → dedupe → match → notify → record

Every step is delegated. This module's job is sequencing, transaction
boundaries, and making sure a ``scrape_runs`` row is written whatever happens —
including when the scan blows up, which is precisely when the telemetry matters
most.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import NotFoundError, PlatformError
from app.core.logging import get_logger, log_context
from app.core.metrics import (
    jobs_duplicate,
    jobs_found,
    jobs_new,
    pages_scraped,
    render_duration,
    scrape_duration,
)
from app.extractors.pipeline import ExtractionPipeline, ExtractionResult
from app.matcher.service import MatchingService
from app.models.company import Company
from app.models.enums import ATSType, ScrapeStatus
from app.models.job import Job
from app.models.scrape_run import ScrapeRun
from app.normalization.service import normalize_jobs
from app.notifications.dispatcher import NotificationDispatcher
from app.repositories.company import CompanyRepository
from app.repositories.job import JobRepository
from app.repositories.scrape_run import ScrapeRunRepository
from app.repositories.user import UserRepository
from app.scrapers.fetcher import Fetcher
from app.utils.time import utcnow

logger = get_logger(__name__)


@dataclass(slots=True)
class ScanReport:
    company_id: uuid.UUID
    company_name: str
    status: ScrapeStatus
    jobs_found: int = 0
    jobs_new: int = 0
    duplicates: int = 0
    notifications: int = 0
    extraction_tier: str | None = None
    confidence: float = 0.0
    llm_calls: int = 0
    llm_cost_usd: float = 0.0
    duration_ms: int = 0
    trail: list[str] = field(default_factory=list)
    error: str | None = None
    #: True when this was the company's first successful scan, so the board was
    #: recorded as a baseline and nothing was delivered.
    baseline: bool = False

    @property
    def succeeded(self) -> bool:
        return self.status in (ScrapeStatus.SUCCESS, ScrapeStatus.PARTIAL)


class ScanService:
    def __init__(
        self,
        session: Session,
        *,
        fetcher: Fetcher | None = None,
        dispatcher: NotificationDispatcher | None = None,
    ) -> None:
        self.session = session
        self.settings = get_settings()
        self.companies = CompanyRepository(session)
        self.jobs = JobRepository(session)
        self.runs = ScrapeRunRepository(session)
        self.users = UserRepository(session)
        self._fetcher = fetcher
        self._owns_fetcher = fetcher is None
        # Injected so a test can substitute a recording channel, and so a
        # future caller can dispatch through a different transport without
        # this class knowing about it.
        self._dispatcher = dispatcher

    def __enter__(self) -> ScanService:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_fetcher and self._fetcher is not None:
            self._fetcher.close()
            self._fetcher = None

    @property
    def fetcher(self) -> Fetcher:
        if self._fetcher is None:
            self._fetcher = Fetcher()
        return self._fetcher

    # -- Entry points ------------------------------------------------------

    def scan_due(self, *, limit: int = 500) -> list[ScanReport]:
        companies = self.companies.due_for_scrape(limit=limit)
        logger.info("scan.batch_start", due=len(companies))
        return [self.scan_company(company.id) for company in companies]

    def scan_company(
        self, company_id: uuid.UUID, *, force_llm: bool = False, notify: bool = True
    ) -> ScanReport:
        company = self.companies.get(company_id)
        if company is None:
            raise NotFoundError("company not found", company_id=str(company_id))

        started = utcnow()
        run = self.runs.open_run(company.id)
        report = ScanReport(
            company_id=company.id,
            company_name=company.name,
            status=ScrapeStatus.RUNNING,
        )

        with log_context(company=company.name, company_id=str(company.id), run_id=str(run.id)):
            try:
                self._execute(company, run, report, force_llm=force_llm, notify=notify)
            except PlatformError as exc:
                self._fail(company, run, report, exc)
            except Exception as exc:  # pragma: no cover - unexpected paths
                logger.exception("scan.unexpected_error")
                self._fail(company, run, report, exc)
            finally:
                report.duration_ms = int((utcnow() - started).total_seconds() * 1000)
                scrape_duration.labels(company.ats_type).observe(
                    report.duration_ms / 1000
                )
                # Committed here rather than by the caller: a failed scan's
                # telemetry and backoff must survive even though the scan
                # itself did not succeed.
                self.session.commit()

        logger.info(
            "scan.finished",
            status=report.status,
            found=report.jobs_found,
            new=report.jobs_new,
            notifications=report.notifications,
            tier=report.extraction_tier,
            cost_usd=report.llm_cost_usd,
            trail=report.trail,
        )
        return report

    # -- Pipeline ----------------------------------------------------------

    def _execute(
        self,
        company: Company,
        run: ScrapeRun,
        report: ScanReport,
        *,
        force_llm: bool,
        notify: bool,
    ) -> None:
        # 0. Identify the platform, if we have not already ------------------
        # Detection lives here rather than in the caller so that every entry
        # point gets it: the CLI used to detect and the Celery task did not,
        # which left every scheduled scan running against `ats_type=unknown`.
        # That silently downgraded Microsoft, Adobe and Visa from their APIs to
        # the generic HTML reader, and from there to the LLM — the scan failed,
        # burned model calls, and looked like a scraping bug.
        if company.ats_type is ATSType.UNKNOWN:
            self._detect(company)

        # 1. Extract -------------------------------------------------------
        pipeline = ExtractionPipeline(self.session, self.fetcher)
        extraction: ExtractionResult = pipeline.run(company, force_llm=force_llm)

        report.trail = extraction.trail
        report.extraction_tier = str(extraction.tier)
        report.confidence = extraction.confidence
        report.llm_calls = extraction.usage.calls
        report.llm_cost_usd = extraction.usage.cost_usd

        run.strategy_used = extraction.strategy
        run.extraction_tier = extraction.tier
        run.selector_version = extraction.selector_version
        run.confidence = extraction.confidence
        run.fetch_ms = extraction.fetch_ms
        run.render_ms = extraction.render_ms
        run.llm_calls = extraction.usage.calls
        run.llm_tokens_in = extraction.usage.input_tokens
        run.llm_tokens_out = extraction.usage.output_tokens
        run.llm_cost_usd = extraction.usage.cost_usd

        if extraction.render_ms:
            render_duration.observe(extraction.render_ms / 1000)

        if not extraction.jobs:
            pages_scraped.labels(company.ats_type, str(extraction.strategy), "empty").inc()
            report.status = ScrapeStatus.FAILED
            report.error = "; ".join(extraction.score.reasons) or "no postings extracted"
            self.runs.close_run(run, ScrapeStatus.FAILED, report.error)
            company.last_error = report.error
            company.schedule_next(failed=True)
            self._deactivate_if_hopeless(company)
            return

        pages_scraped.labels(company.ats_type, str(extraction.strategy), "success").inc()

        # 2. Normalise -----------------------------------------------------
        normalized = normalize_jobs(
            extraction.jobs, company=company, tier=extraction.tier
        )
        report.jobs_found = len(normalized)
        run.jobs_found = len(normalized)
        jobs_found.inc(len(normalized))

        # 3. Deduplicate ---------------------------------------------------
        # The database decides what is new (AD-6); everything already known
        # just gets its last-seen timestamp bumped.
        fresh = self.jobs.insert_new(normalized)
        self.jobs.touch_seen(company.id, [job.content_hash for job in normalized])

        report.jobs_new = len(fresh)
        report.duplicates = len(normalized) - len(fresh)
        run.jobs_new = len(fresh)
        run.jobs_duplicate = report.duplicates
        jobs_new.inc(len(fresh))
        jobs_duplicate.inc(report.duplicates)

        # 4/5. Match and notify -------------------------------------------
        # A company's first successful scan is a *baseline*. Every posting on
        # the board is "new" to us, but none of it is news to the user — they
        # just added the company and can see the board themselves. Notifying
        # here means a few hundred emails per company registered, which is how
        # people end up filtering the whole thing to spam on day one.
        #
        # Matching still runs and scores are still stored, so `GET /jobs/new`
        # is populated immediately; only delivery is suppressed.
        # Fetch detail pages for the shortlist before scoring, so experience
        # requirements stated only on the posting's own page are visible to
        # the matcher rather than invisible to it.
        if fresh and self.settings.enrich_details:
            from app.scrapers.registry import get_adapter
            from app.services.enrichment import JobEnricher

            scraper = get_adapter(company.ats_type)(company, self.fetcher)
            profiles = [u.profile for u in self.users.list_with_profiles() if u.profile]
            if profiles:
                JobEnricher(
                    self.session, scraper, limit=self.settings.max_details_per_run
                ).enrich(fresh, profiles)

        baseline = self._is_first_scan(company, run)
        if fresh:
            report.notifications = self._match_and_notify(
                fresh, extraction, deliver=notify and not baseline
            )
            run.notifications_sent = report.notifications
        if baseline:
            report.baseline = True
            report.trail.append(f"baseline:{len(fresh)}_jobs_recorded_silently")
            logger.info("scan.baseline", jobs=len(fresh))

        # 6. Record --------------------------------------------------------
        status = (
            ScrapeStatus.SUCCESS
            if extraction.score.is_acceptable
            else ScrapeStatus.PARTIAL
        )
        report.status = status
        self.runs.close_run(run, status)
        company.schedule_next(failed=False)

    def _detect(self, company: Company) -> None:
        """Work out which platform this is, and remember the answer."""
        from app.scrapers.detection import detect

        try:
            found = detect(company.career_url, self.fetcher)
        except PlatformError as exc:
            logger.warning("scan.detection_failed", error=str(exc))
            return

        company.ats_type = found.ats_type
        company.scraping_strategy = found.strategy
        if found.board_token:
            company.board_token = found.board_token
        self.session.flush()
        logger.info(
            "scan.detected",
            ats=str(found.ats_type),
            evidence=found.evidence,
            confidence=found.confidence,
        )

    def _is_first_scan(self, company: Company, current: ScrapeRun) -> bool:
        """Has this company ever produced a usable scan before?

        Keyed on prior *successful* runs rather than on ``last_scraped_at``,
        which is stamped on failures too — a company that 404'd four times
        would otherwise be treated as established and flood the user the
        moment it finally worked.
        """
        previous = (
            self.session.query(ScrapeRun)
            .filter(
                ScrapeRun.company_id == company.id,
                ScrapeRun.id != current.id,
                ScrapeRun.status.in_([ScrapeStatus.SUCCESS, ScrapeStatus.PARTIAL]),
            )
            .first()
        )
        return previous is None

    def _match_and_notify(
        self, fresh: list[Job], extraction: ExtractionResult, *, deliver: bool = True
    ) -> int:
        """Score new postings for every user, and deliver when appropriate.

        Matching is per profile because thresholds and preferences are per
        profile; a job that clears one user's bar may not clear another's.
        """
        matching = MatchingService(self.session, tally=extraction.usage)
        dispatcher = self._dispatcher or NotificationDispatcher(self.session)
        sent = 0

        for user in self.users.list_with_profiles():
            profile = user.profile
            if profile is None:
                continue

            scored = matching.score_jobs(fresh, profile)
            if not deliver:
                continue
            for entry in matching.matches_above_threshold(scored, profile):
                sent += len(dispatcher.notify(user, entry.job, entry.result))

        return sent

    # -- Failure handling --------------------------------------------------

    def _fail(
        self, company: Company, run: ScrapeRun, report: ScanReport, exc: Exception
    ) -> None:
        message = str(exc)
        report.status = ScrapeStatus.FAILED
        report.error = message
        pages_scraped.labels(company.ats_type, str(company.scraping_strategy), "error").inc()

        self.runs.close_run(run, ScrapeStatus.FAILED, message[:2000])
        company.last_error = message[:2000]
        company.schedule_next(failed=True)
        self._deactivate_if_hopeless(company)

        logger.error("scan.failed", error=message, failures=company.consecutive_failures)

    def _deactivate_if_hopeless(self, company: Company) -> None:
        """Stop scanning a company that has failed continuously.

        The backoff in ``Company.schedule_next`` already spaces attempts out;
        this is the terminal case. Deactivating (rather than deleting) keeps
        the history and lets a user re-enable it after fixing the URL.
        """
        if company.consecutive_failures >= self.settings.scrape_max_consecutive_failures:
            self.companies.deactivate(
                company,
                f"deactivated after {company.consecutive_failures} consecutive failures",
            )
            logger.error("scan.company_deactivated", company=company.name)
