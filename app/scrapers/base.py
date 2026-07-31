"""Scraper contract.

Every adapter — whether it calls a documented JSON API or reads selectors off
rendered HTML — returns the same :class:`RawJob` shape. Nothing downstream
(normalisation, dedup, matching, notification) knows or cares which platform a
posting came from. That single convergence point is what keeps "add support for
another ATS" a self-contained change.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from app.core.logging import get_logger
from app.models.enums import ATSType, ExtractionTier, ScrapingStrategy
from app.scrapers.fetcher import Fetcher, FetchResult

if TYPE_CHECKING:
    from app.models.company import Company

logger = get_logger(__name__)


@dataclass(slots=True)
class RawJob:
    """A posting as the source described it, before any interpretation.

    Fields are strings because that is what sites give us; turning
    ``"3 days ago"`` into a date and ``"$120k–150k"`` into numbers is
    normalisation's job, and keeping the two stages apart means a parsing bug
    can be fixed and replayed against ``raw`` without re-scraping.
    """

    title: str
    url: str | None = None
    external_id: str | None = None
    location: str | None = None
    description: str | None = None
    requirements: str | None = None
    department: str | None = None
    employment_type: str | None = None
    salary: str | None = None
    posted_at: str | None = None
    remote: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def is_usable(self) -> bool:
        """A posting without a title or a link is not a posting.

        Enforced here rather than at each call site because every extraction
        tier can produce fragments — a stray heading matched by an overly broad
        selector, an API record for a closed requisition — and they must not
        reach the database.
        """
        return bool(self.title and self.title.strip() and self.url)


@dataclass(slots=True)
class JobDetail:
    """What a job's own page adds beyond its listing row.

    Listings are deliberately thin — a title, a location, sometimes a date.
    The requirement that decides whether a job is worth applying for ("8+
    years of experience") lives on the detail page, so without this the
    experience filter has nothing to read.
    """

    description: str | None = None
    requirements: str | None = None
    employment_type: str | None = None
    posted_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def is_useful(self) -> bool:
        return bool(self.description or self.requirements)


@dataclass(slots=True)
class ScrapeOutcome:
    """What one adapter produced for one company."""

    jobs: list[RawJob]
    tier: ExtractionTier
    strategy: ScrapingStrategy
    confidence: float
    fetch_ms: int = 0
    render_ms: int = 0
    selector_version: int | None = None
    llm_calls: int = 0
    llm_tokens_in: int = 0
    llm_tokens_out: int = 0
    llm_cost_usd: float = 0.0

    @property
    def job_count(self) -> int:
        return len(self.jobs)


class BaseScraper(ABC):
    """Template for every ATS adapter.

    Subclasses override :meth:`extract_jobs` and, when the platform exposes a
    JSON API, :meth:`fetch`. The three-step ``fetch → extract_jobs → normalize``
    shape is fixed here so that adapters cannot invent their own control flow
    and quietly skip, say, the usability filter.
    """

    ats_type: ClassVar[ATSType] = ATSType.UNKNOWN
    default_strategy: ClassVar[ScrapingStrategy] = ScrapingStrategy.AUTO
    #: True when the adapter can satisfy tier 1 without touching HTML.
    supports_api: ClassVar[bool] = False
    #: Hostname fragments that identify this platform without any request.
    host_markers: ClassVar[tuple[str, ...]] = ()
    #: Strings whose presence in the page body identify this platform.
    body_markers: ClassVar[tuple[str, ...]] = ()

    def __init__(self, company: Company, fetcher: Fetcher) -> None:
        self.company = company
        self.fetcher = fetcher
        self.log = logger.bind(scraper=self.ats_type, company=company.name)

    # -- Steps -------------------------------------------------------------

    def fetch(self) -> FetchResult:
        """Retrieve the listing. Overridden by API-backed adapters."""
        return self.fetcher.fetch(
            self.company.career_url,
            strategy=self._effective_strategy(),
            wait_for_selector=self.wait_for_selector(),
        )

    @abstractmethod
    def extract_jobs(self, result: FetchResult) -> list[RawJob]:
        """Turn a fetched page or payload into raw postings."""

    def normalize(self, raws: list[RawJob]) -> list[Any]:
        """Convert raw postings into persistable ``Job`` rows.

        Delegated to the normalisation service rather than implemented per
        adapter: date formats and location strings vary by *site*, not by ATS,
        so per-adapter normalisation would duplicate the same parsers nine
        times and let them drift apart.
        """
        from app.normalization.service import normalize_jobs

        return normalize_jobs(raws, company=self.company, tier=self.tier)

    # -- Template method ---------------------------------------------------

    def scrape(self) -> ScrapeOutcome:
        result = self.fetch()
        jobs = [job for job in self.extract_jobs(result) if job.is_usable()]
        self.log.info(
            "scrape.extracted",
            found=len(jobs),
            tier=self.tier,
            strategy=result.strategy,
        )
        return ScrapeOutcome(
            jobs=jobs,
            tier=self.tier,
            strategy=result.strategy,
            confidence=self.confidence_for(jobs),
            fetch_ms=result.fetch_ms,
            render_ms=result.render_ms,
        )

    # -- Hooks -------------------------------------------------------------

    @property
    def tier(self) -> ExtractionTier:
        return ExtractionTier.API if self.supports_api else ExtractionTier.CSS_SELECTOR

    def wait_for_selector(self) -> str | None:
        """Selector to await before reading a rendered DOM, if any."""
        return None

    def fetch_detail(self, url: str) -> JobDetail | None:
        """Fetch one posting's own page. Overridden where an API exists.

        The default reads schema.org ``JobPosting`` markup, which a large share
        of detail pages publish for search engines and which is far more
        reliable than guessing at the layout. Adapters whose sites render the
        detail client-side (Apple) override this with their JSON endpoint.
        """
        from app.extractors.embedded_json import extract_job_detail

        try:
            result = self.fetcher.fetch(url, strategy=ScrapingStrategy.HTTP)
        except Exception as exc:
            self.log.debug("detail.fetch_failed", url=url, error=str(exc))
            return None
        return extract_job_detail(result.text)

    def confidence_for(self, jobs: list[RawJob]) -> float:
        """How much to trust this extraction.

        An API adapter is trusted outright — the payload is contractual. HTML
        adapters defer to the shared scorer, which inspects field completeness
        rather than assuming a non-empty list means success.
        """
        if not jobs:
            return 0.0
        if self.supports_api:
            return 1.0
        from app.extractors.validation import score_extraction

        return score_extraction(jobs).confidence

    def _effective_strategy(self) -> ScrapingStrategy:
        """Company override beats adapter default beats AUTO."""
        if self.company.scraping_strategy not in (
            ScrapingStrategy.AUTO,
            None,
        ):
            return self.company.scraping_strategy
        return self.default_strategy

    # -- Detection support -------------------------------------------------

    @classmethod
    def matches_host(cls, url: str) -> bool:
        lowered = url.lower()
        return any(marker in lowered for marker in cls.host_markers)

    @classmethod
    def matches_body(cls, body: str) -> bool:
        lowered = body[:200_000].lower()
        return any(marker in lowered for marker in cls.body_markers)

    def _job_uuid(self) -> uuid.UUID:  # pragma: no cover - trivial helper
        return uuid.uuid4()
