"""Selector learning.

The feature the whole platform is organised around: when a site cannot be
scraped by any deterministic means, ask a model *how* to scrape it, verify the
answer against the page in front of us, and persist it so no future scrape of
that domain needs a model again.

Verification is the part that is easy to skip and must not be. A model's stated
confidence is a guess about its own output; running the selectors and scoring
the result is evidence. Only evidence gets stored.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import LLMBudgetExceededError, LLMError, LLMResponseError
from app.core.logging import get_logger
from app.core.metrics import selector_confidence, selector_regenerations
from app.extractors.llm_extractor import generate_selectors
from app.extractors.selector_extractor import SelectorSet, extract_with_selectors
from app.extractors.validation import ExtractionScore, score_extraction
from app.llm.client import LLMClient, UsageTally
from app.llm.prompts import SELECTOR_PROMPT_VERSION
from app.models.enums import SelectorOrigin
from app.models.selector import Selector
from app.repositories.selector import SelectorRepository
from app.scrapers.base import RawJob

logger = get_logger(__name__)


@dataclass(slots=True)
class LearningResult:
    selector: Selector | None
    jobs: list[RawJob]
    score: ExtractionScore
    persisted: bool

    @property
    def succeeded(self) -> bool:
        return bool(self.jobs) and self.score.is_acceptable


class SelectorLearner:
    """Generates, verifies, and versions extraction strategies."""

    def __init__(self, session: Session, client: LLMClient) -> None:
        self.session = session
        self.client = client
        self.selectors = SelectorRepository(session)
        self.settings = get_settings()

    def learn(
        self,
        *,
        website: str,
        html: str,
        url: str,
        requires_render: bool = False,
        tally: UsageTally | None = None,
    ) -> LearningResult:
        """Generate a strategy for ``website`` and store it if it works.

        The returned jobs are the ones extracted during verification, so a
        successful learn also satisfies the current scrape — the model is not
        paid for twice.
        """
        empty = ExtractionScore(confidence=0.0, jobs_found=0, reasons=["not attempted"])

        try:
            generated = generate_selectors(html, url, client=self.client, tally=tally)
        except LLMBudgetExceededError:
            # A global condition, not a generation failure. Swallowing it here
            # would let the caller fall through to one-shot extraction and
            # spend another call it also cannot afford.
            raise
        except (LLMError, LLMResponseError) as exc:
            logger.warning("learner.generation_failed", website=website, error=str(exc))
            return LearningResult(None, [], empty, persisted=False)

        candidate = generated.selectors
        jobs = [
            job
            for job in extract_with_selectors(html, url, candidate)
            if job.is_usable()
        ]
        score = score_extraction(jobs)

        logger.info(
            "learner.verified",
            website=website,
            jobs=len(jobs),
            measured_confidence=score.confidence,
            claimed_confidence=generated.claimed_confidence,
            reasons=score.reasons,
        )

        if not score.is_acceptable:
            # Storing a strategy that demonstrably does not work would poison
            # tier 3 for this domain: every future scrape would try it, fail,
            # and relearn — turning a one-off cost into a recurring one.
            logger.warning(
                "learner.rejected",
                website=website,
                confidence=score.confidence,
                threshold=self.settings.extraction_min_confidence,
            )
            return LearningResult(None, jobs, score, persisted=False)

        selector = self._persist(
            website=website,
            candidate=candidate,
            measured_confidence=score.confidence,
            requires_render=requires_render or candidate.requires_render,
            model=generated.model,
            notes=generated.notes,
        )
        return LearningResult(selector, jobs, score, persisted=True)

    def _persist(
        self,
        *,
        website: str,
        candidate: SelectorSet,
        measured_confidence: float,
        requires_render: bool,
        model: str,
        notes: str | None,
    ) -> Selector:
        """Insert the next version and retire the previous one (AD-4)."""
        version = self.selectors.next_version(website)
        selector = Selector(
            website=website,
            selector_version=version,
            strategy=candidate.strategy,
            container_selector=candidate.container,
            title_selector=candidate.title,
            url_selector=candidate.url,
            location_selector=candidate.location,
            description_selector=candidate.description,
            date_selector=candidate.date,
            department_selector=candidate.department,
            requires_render=requires_render,
            # Seeded with the *measured* score, not the model's claim.
            confidence_score=measured_confidence,
            success_count=1,
            origin=SelectorOrigin.LLM,
            llm_model=model,
            notes=f"[prompt v{SELECTOR_PROMPT_VERSION}] {notes or ''}".strip(),
        )
        self.selectors.promote(selector)
        self.selectors.prune(website, self.settings.selector_max_versions_retained)

        selector_regenerations.labels(website).inc()
        selector_confidence.labels(website).set(measured_confidence)

        logger.info(
            "learner.stored",
            website=website,
            version=version,
            confidence=measured_confidence,
            container=candidate.container,
        )
        return selector
