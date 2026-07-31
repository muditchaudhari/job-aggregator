"""Selector health tracking.

Every extraction attempt that used a stored selector reports back here. The
accumulated record is what decides when a site needs relearning — and, just as
importantly, when it does not.

The trigger is *consecutive* failures rather than a success-rate threshold. A
site that fails one scrape in twenty (a deploy, a timeout, a cookie wall on one
request) has a 95% success rate and needs no intervention; a site that has
failed the last three in a row is genuinely broken. Rate-based triggers get
this backwards and relearn healthy sites while tolerating dead ones.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.metrics import selector_confidence
from app.extractors.validation import ExtractionScore
from app.models.selector import Selector
from app.repositories.selector import SelectorRepository

logger = get_logger(__name__)


class SelectorFeedback:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.selectors = SelectorRepository(session)
        self.settings = get_settings()

    def record(self, selector: Selector, score: ExtractionScore) -> None:
        if score.is_acceptable:
            selector.record_success(score.confidence)
            logger.debug(
                "selector.success",
                website=selector.website,
                version=selector.selector_version,
                confidence=selector.confidence_score,
            )
        else:
            selector.record_failure()
            logger.warning(
                "selector.failure",
                website=selector.website,
                version=selector.selector_version,
                consecutive=selector.consecutive_failures,
                jobs_found=score.jobs_found,
                reasons=score.reasons,
            )
        selector_confidence.labels(selector.website).set(selector.confidence_score)
        self.session.flush()

    def should_regenerate(self, selector: Selector | None) -> bool:
        """Is it time to spend an LLM call on this domain?

        ``None`` means no strategy exists yet, which is the first-contact case
        and always warrants learning.
        """
        if selector is None:
            return True
        return (
            selector.consecutive_failures
            >= self.settings.selector_regenerate_after_failures
        )

    def degraded_websites(self, *, threshold: float | None = None) -> list[str]:
        """Domains worth relearning proactively.

        Used by the nightly maintenance task so that a slowly rotting selector
        is fixed off the critical path, rather than during a user-facing scan
        where the LLM call adds seconds of latency.
        """
        limit = threshold if threshold is not None else self.settings.extraction_min_confidence
        return [s.website for s in self.selectors.list_degraded(threshold=limit)]
