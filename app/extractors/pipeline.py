"""The extraction ladder.

Tries every deterministic strategy before spending a model call, and descends
only when the tier above produced something that failed validation (AD-3):

    1. official API        adapter.supports_api
    2. embedded JSON       JSON-LD, __NEXT_DATA__, framework state
    3. CSS selectors       adapter built-ins, then the learned strategy
    4. XPath               the learned strategy, when it is XPath-shaped
    5. LLM                 learn a strategy; failing that, extract this page

Ownership sits here rather than in the adapters so that all nine adapters get
identical fallback behaviour, and so the decision "was this good enough?" is
made in exactly one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import FetchError, LLMBudgetExceededError, LLMError
from app.core.logging import get_logger
from app.core.metrics import extraction_attempts
from app.extractors.embedded_json import extract_embedded_jobs
from app.extractors.llm_extractor import extract_fields_directly
from app.extractors.selector_extractor import SelectorSet, extract_with_selectors
from app.extractors.validation import ExtractionScore, score_extraction
from app.learning.feedback import SelectorFeedback
from app.learning.selector_learner import SelectorLearner
from app.llm.client import LLMClient, UsageTally, get_llm_client
from app.models.company import Company
from app.models.enums import ExtractionTier, ScrapingStrategy, SelectorStrategy
from app.models.selector import Selector
from app.repositories.selector import SelectorRepository
from app.scrapers.base import BaseScraper, RawJob
from app.scrapers.fetcher import Fetcher, FetchResult
from app.scrapers.registry import get_adapter

logger = get_logger(__name__)


@dataclass(slots=True)
class ExtractionResult:
    jobs: list[RawJob]
    tier: ExtractionTier
    score: ExtractionScore
    strategy: ScrapingStrategy = ScrapingStrategy.AUTO
    selector_version: int | None = None
    fetch_ms: int = 0
    render_ms: int = 0
    usage: UsageTally = field(default_factory=UsageTally)
    #: Every tier attempted, in order, with its outcome. The audit trail for
    #: "why did this cost an LLM call?".
    trail: list[str] = field(default_factory=list)

    @property
    def confidence(self) -> float:
        return self.score.confidence

    @property
    def succeeded(self) -> bool:
        return bool(self.jobs) and self.score.is_acceptable


class ExtractionPipeline:
    def __init__(
        self,
        session: Session,
        fetcher: Fetcher,
        *,
        llm_client: LLMClient | None = None,
    ) -> None:
        self.session = session
        self.fetcher = fetcher
        self.settings = get_settings()
        self.selectors = SelectorRepository(session)
        self.feedback = SelectorFeedback(session)
        self._llm_client = llm_client

    @property
    def llm(self) -> LLMClient:
        if self._llm_client is None:
            self._llm_client = get_llm_client()
        return self._llm_client

    # -- Entry point -------------------------------------------------------

    def run(self, company: Company, *, force_llm: bool = False) -> ExtractionResult:
        """Extract postings for one company, climbing down the ladder."""
        usage = UsageTally()
        trail: list[str] = []
        adapter_class = get_adapter(company.ats_type)
        scraper = adapter_class(company, self.fetcher)

        # -- Tier 1: official API -----------------------------------------
        if adapter_class.supports_api and not force_llm:
            result = self._try_api(scraper, usage, trail)
            if result is not None:
                return result

        # -- Fetch the page once; tiers 2-5 all read the same HTML ---------
        fetch = self._fetch_html(scraper, company)
        if fetch is None:
            return ExtractionResult(
                jobs=[],
                tier=ExtractionTier.CSS_SELECTOR,
                score=ExtractionScore(0.0, 0, reasons=["page could not be fetched"]),
                usage=usage,
                trail=[*trail, "fetch:failed"],
            )

        base_url = fetch.final_url or fetch.url
        stored = self.selectors.get_active(company.website)

        if not force_llm:
            # -- Tier 2: embedded JSON ------------------------------------
            result = self._try_embedded_json(fetch, base_url, usage, trail)
            if result is not None:
                return result

            # -- Tier 3: adapter built-in CSS -----------------------------
            result = self._try_builtin(scraper, fetch, usage, trail)
            if result is not None:
                return result

            # -- Tiers 3/4: the learned strategy --------------------------
            if stored is not None:
                result = self._try_stored(stored, company, fetch, base_url, usage, trail)
                if result is not None:
                    return result

        # -- Tier 5: the LLM ----------------------------------------------
        if not self.feedback.should_regenerate(stored) and not force_llm:
            # The stored selector is not yet failing consistently enough to be
            # worth a model call. Report the failure honestly rather than
            # spending money on what may be a one-off bad render.
            trail.append("llm:skipped_not_yet_degraded")
            return ExtractionResult(
                jobs=[],
                tier=ExtractionTier.CSS_SELECTOR,
                score=ExtractionScore(0.0, 0, reasons=["stored selector returned nothing"]),
                strategy=fetch.strategy,
                selector_version=stored.selector_version if stored else None,
                fetch_ms=fetch.fetch_ms,
                render_ms=fetch.render_ms,
                usage=usage,
                trail=trail,
            )

        return self._try_llm(company, fetch, base_url, usage, trail)

    # -- Tiers -------------------------------------------------------------

    def _try_api(
        self, scraper: BaseScraper, usage: UsageTally, trail: list[str]
    ) -> ExtractionResult | None:
        try:
            outcome = scraper.scrape()
        except FetchError as exc:
            # An API adapter whose API is unreachable is not a dead end — the
            # company still has a public careers page we can read as HTML.
            extraction_attempts.labels(ExtractionTier.API, "error").inc()
            trail.append(f"api:error({type(exc).__name__})")
            logger.warning("pipeline.api_failed", error=str(exc))
            return None

        score = score_extraction(outcome.jobs)
        if not score.is_acceptable:
            extraction_attempts.labels(ExtractionTier.API, "rejected").inc()
            trail.append(f"api:rejected({score.jobs_found} jobs)")
            return None

        extraction_attempts.labels(ExtractionTier.API, "success").inc()
        trail.append(f"api:ok({score.jobs_found} jobs)")
        return ExtractionResult(
            jobs=outcome.jobs,
            tier=ExtractionTier.API,
            score=score,
            strategy=ScrapingStrategy.API,
            fetch_ms=outcome.fetch_ms,
            usage=usage,
            trail=trail,
        )

    def _try_embedded_json(
        self, fetch: FetchResult, base_url: str, usage: UsageTally, trail: list[str]
    ) -> ExtractionResult | None:
        jobs = [job for job in extract_embedded_jobs(fetch.text, base_url) if job.is_usable()]
        score = score_extraction(jobs)
        if not score.is_acceptable:
            extraction_attempts.labels(ExtractionTier.EMBEDDED_JSON, "rejected").inc()
            trail.append(f"embedded_json:miss({score.jobs_found})")
            return None

        extraction_attempts.labels(ExtractionTier.EMBEDDED_JSON, "success").inc()
        trail.append(f"embedded_json:ok({score.jobs_found} jobs)")
        return ExtractionResult(
            jobs=jobs,
            tier=ExtractionTier.EMBEDDED_JSON,
            score=score,
            strategy=fetch.strategy,
            fetch_ms=fetch.fetch_ms,
            render_ms=fetch.render_ms,
            usage=usage,
            trail=trail,
        )

    def _try_builtin(
        self,
        scraper: BaseScraper,
        fetch: FetchResult,
        usage: UsageTally,
        trail: list[str],
    ) -> ExtractionResult | None:
        jobs = [job for job in scraper.extract_jobs(fetch) if job.is_usable()]
        score = score_extraction(jobs)
        if not score.is_acceptable:
            trail.append(f"builtin_css:miss({score.jobs_found})")
            return None

        # The adapter's own tier, not a hardcoded one. Adapters that read
        # structured data off the page (Phenom's `phApp.ddo`) report
        # EMBEDDED_JSON, and mislabelling that as a CSS selector makes the
        # run report — and the selector-health metrics — quietly wrong.
        tier = scraper.tier
        extraction_attempts.labels(tier, "success").inc()
        trail.append(f"builtin:{tier}({score.jobs_found} jobs)")
        return ExtractionResult(
            jobs=jobs,
            tier=tier,
            score=score,
            strategy=fetch.strategy,
            fetch_ms=fetch.fetch_ms,
            render_ms=fetch.render_ms,
            usage=usage,
            trail=trail,
        )

    def _try_stored(
        self,
        stored: Selector,
        company: Company,
        fetch: FetchResult,
        base_url: str,
        usage: UsageTally,
        trail: list[str],
    ) -> ExtractionResult | None:
        candidate = SelectorSet.from_model(stored)
        tier = (
            ExtractionTier.XPATH
            if candidate.strategy is SelectorStrategy.XPATH
            else ExtractionTier.CSS_SELECTOR
        )

        # The learner may have discovered this site needs a render while the
        # current fetch used plain HTTP. Re-fetch once rather than failing and
        # relearning — the selectors are probably fine.
        if stored.requires_render and fetch.strategy is not ScrapingStrategy.PLAYWRIGHT:
            trail.append("stored:re-fetch_with_render")
            try:
                fetch = self.fetcher.fetch(
                    company.career_url, strategy=ScrapingStrategy.PLAYWRIGHT
                )
                base_url = fetch.final_url or fetch.url
            except FetchError as exc:
                logger.warning("pipeline.render_refetch_failed", error=str(exc))

        jobs = [
            job for job in extract_with_selectors(fetch.text, base_url, candidate)
            if job.is_usable()
        ]
        score = score_extraction(jobs)
        self.feedback.record(stored, score)

        if not score.is_acceptable:
            extraction_attempts.labels(tier, "rejected").inc()
            trail.append(f"stored_v{stored.selector_version}:miss({score.jobs_found})")
            return None

        extraction_attempts.labels(tier, "success").inc()
        trail.append(f"stored_v{stored.selector_version}:ok({score.jobs_found} jobs)")
        return ExtractionResult(
            jobs=jobs,
            tier=tier,
            score=score,
            strategy=fetch.strategy,
            selector_version=stored.selector_version,
            fetch_ms=fetch.fetch_ms,
            render_ms=fetch.render_ms,
            usage=usage,
            trail=trail,
        )

    def _try_llm(
        self,
        company: Company,
        fetch: FetchResult,
        base_url: str,
        usage: UsageTally,
        trail: list[str],
    ) -> ExtractionResult:
        """Learn a strategy; fall back to one-shot extraction if that fails."""
        empty = ExtractionScore(0.0, 0, reasons=["all extraction tiers failed"])

        if not self.llm.is_available:
            trail.append("llm:unavailable")
            return ExtractionResult(
                jobs=[], tier=ExtractionTier.LLM, score=empty, usage=usage, trail=trail
            )

        # A learned selector is only reusable if it resolves against markup we
        # can reproduce. Learning from a rendered DOM means future scrapes must
        # render too, so that fact is recorded with the selector.
        needs_render = fetch.strategy is ScrapingStrategy.PLAYWRIGHT

        learner = SelectorLearner(self.session, self.llm)
        try:
            learned = learner.learn(
                website=company.website,
                html=fetch.text,
                url=base_url,
                requires_render=needs_render,
                tally=usage,
            )
        except LLMBudgetExceededError as exc:
            extraction_attempts.labels(ExtractionTier.LLM, "budget").inc()
            trail.append("llm:budget_exceeded")
            logger.warning("pipeline.llm_budget", company=company.name, error=str(exc))
            return ExtractionResult(
                jobs=[], tier=ExtractionTier.LLM, score=empty, usage=usage, trail=trail
            )

        if learned.succeeded:
            extraction_attempts.labels(ExtractionTier.LLM, "success").inc()
            trail.append(
                f"llm:learned_v{learned.selector.selector_version if learned.selector else '?'}"
            )
            return ExtractionResult(
                jobs=learned.jobs,
                tier=ExtractionTier.LLM,
                score=learned.score,
                strategy=fetch.strategy,
                selector_version=(
                    learned.selector.selector_version if learned.selector else None
                ),
                fetch_ms=fetch.fetch_ms,
                render_ms=fetch.render_ms,
                usage=usage,
                trail=trail,
            )

        # Selector generation failed. Get this scan's postings directly so the
        # user is not left with nothing while the learner keeps trying on
        # subsequent runs.
        trail.append("llm:selector_generation_failed")
        try:
            jobs = [
                job
                for job in extract_fields_directly(
                    fetch.text, base_url, client=self.llm, tally=usage
                )
                if job.is_usable()
            ]
        except (LLMError, LLMBudgetExceededError) as exc:
            extraction_attempts.labels(ExtractionTier.LLM, "error").inc()
            trail.append(f"llm:direct_failed({type(exc).__name__})")
            logger.error("pipeline.llm_failed", company=company.name, error=str(exc))
            return ExtractionResult(
                jobs=[], tier=ExtractionTier.LLM, score=empty, usage=usage, trail=trail
            )

        score = score_extraction(jobs)
        extraction_attempts.labels(
            ExtractionTier.LLM, "success" if score.is_acceptable else "rejected"
        ).inc()
        trail.append(f"llm:direct({score.jobs_found} jobs)")
        return ExtractionResult(
            jobs=jobs,
            tier=ExtractionTier.LLM,
            score=score,
            strategy=fetch.strategy,
            fetch_ms=fetch.fetch_ms,
            render_ms=fetch.render_ms,
            usage=usage,
            trail=trail,
        )

    # -- Helpers -----------------------------------------------------------

    def _fetch_html(self, scraper: BaseScraper, company: Company) -> FetchResult | None:
        """Get the page as HTML, regardless of what the adapter prefers.

        API adapters override ``fetch()`` to return JSON. When their API has
        already failed we need the human-facing page instead, so the fetcher is
        called directly rather than through the adapter.
        """
        try:
            if type(scraper).supports_api:
                return self.fetcher.fetch(
                    company.career_url, strategy=ScrapingStrategy.AUTO
                )
            return scraper.fetch()
        except FetchError as exc:
            logger.warning(
                "pipeline.fetch_failed", company=company.name, error=str(exc)
            )
            return None
