"""Self-learning: selector generation, verification, versioning, regeneration.

The behaviour under test is the one that keeps the platform honest — a model's
claimed confidence is never trusted; only selectors that demonstrably extract
jobs from the page in front of us are stored.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy.orm import Session

from app.extractors.validation import ExtractionScore
from app.learning.feedback import SelectorFeedback
from app.learning.selector_learner import SelectorLearner
from app.llm.client import LLMClient, UsageTally
from app.models.enums import SelectorOrigin
from app.models.selector import Selector
from app.repositories.selector import SelectorRepository
from tests.fixtures.fakes import FakeBudgetTracker, FakeLLMProvider
from tests.fixtures.pages import (
    GENERIC_LISTING_HTML,
    LLM_BAD_SELECTOR_RESPONSE,
    LLM_SELECTOR_RESPONSE,
)

URL = "https://widgets.example.com/careers"
WEBSITE = "example.com"


def _client(responses: list[str]) -> LLMClient:
    return LLMClient(
        provider=FakeLLMProvider(responses), budget=cast(Any, FakeBudgetTracker())
    )


class TestSelectorLearning:
    def test_learns_verifies_and_stores(self, db_session: Session) -> None:
        client = _client([LLM_SELECTOR_RESPONSE])
        learner = SelectorLearner(db_session, client)

        result = learner.learn(
            website=WEBSITE, html=GENERIC_LISTING_HTML, url=URL, tally=UsageTally()
        )

        assert result.succeeded
        assert result.persisted
        assert len(result.jobs) == 3

        stored = SelectorRepository(db_session).get_active(WEBSITE)
        assert stored is not None
        assert stored.selector_version == 1
        assert stored.container_selector == "li.job-item"
        assert stored.origin is SelectorOrigin.LLM
        assert stored.success_count == 1

    def test_stored_confidence_is_measured_not_claimed(self, db_session: Session) -> None:
        """The model claimed 0.92; what gets stored is what we observed."""
        learner = SelectorLearner(db_session, _client([LLM_SELECTOR_RESPONSE]))
        result = learner.learn(website=WEBSITE, html=GENERIC_LISTING_HTML, url=URL)

        assert result.selector is not None
        assert result.selector.confidence_score == result.score.confidence

    def test_confident_but_wrong_selectors_are_rejected(self, db_session: Session) -> None:
        """Storing these would make every future scrape fail and relearn."""
        learner = SelectorLearner(db_session, _client([LLM_BAD_SELECTOR_RESPONSE]))

        result = learner.learn(website=WEBSITE, html=GENERIC_LISTING_HTML, url=URL)

        assert not result.succeeded
        assert not result.persisted
        assert SelectorRepository(db_session).get_active(WEBSITE) is None

    def test_provider_failure_is_survivable(self, db_session: Session) -> None:
        client = LLMClient(
            provider=FakeLLMProvider(broken=True), budget=cast(Any, FakeBudgetTracker())
        )
        result = SelectorLearner(db_session, client).learn(
            website=WEBSITE, html=GENERIC_LISTING_HTML, url=URL
        )
        assert not result.persisted
        assert result.jobs == []

    def test_malformed_response_is_survivable(self, db_session: Session) -> None:
        learner = SelectorLearner(db_session, _client(["not json at all"]))
        assert not learner.learn(
            website=WEBSITE, html=GENERIC_LISTING_HTML, url=URL
        ).persisted


class TestVersioning:
    def test_regeneration_creates_a_new_version_and_retires_the_old(
        self, db_session: Session
    ) -> None:
        learner = SelectorLearner(
            db_session, _client([LLM_SELECTOR_RESPONSE, LLM_SELECTOR_RESPONSE])
        )
        first = learner.learn(website=WEBSITE, html=GENERIC_LISTING_HTML, url=URL)
        second = learner.learn(website=WEBSITE, html=GENERIC_LISTING_HTML, url=URL)

        assert first.selector is not None and second.selector is not None
        assert first.selector.selector_version == 1
        assert second.selector.selector_version == 2

        repository = SelectorRepository(db_session)
        assert repository.get_active(WEBSITE).selector_version == 2
        # History is retained (AD-4) so a regression is revertible.
        assert len(repository.list_versions(WEBSITE)) == 2
        assert first.selector.is_active is False

    def test_pruning_keeps_the_retention_limit(self, db_session: Session) -> None:
        repository = SelectorRepository(db_session)
        for version in range(1, 8):
            repository.add(
                Selector(
                    website=WEBSITE,
                    selector_version=version,
                    container_selector="li",
                    title_selector="a",
                    is_active=False,
                )
            )
        db_session.flush()

        pruned = repository.prune(WEBSITE, keep=3)
        assert pruned == 4
        assert len(repository.list_versions(WEBSITE)) == 3


class TestFeedback:
    def _selector(self, db_session: Session, **kwargs: Any) -> Selector:
        selector = Selector(
            website=WEBSITE,
            selector_version=1,
            container_selector="li.job-item",
            title_selector="a",
            confidence_score=0.9,
            **kwargs,
        )
        db_session.add(selector)
        db_session.flush()
        return selector

    def test_success_raises_confidence_and_clears_the_streak(
        self, db_session: Session
    ) -> None:
        selector = self._selector(db_session, consecutive_failures=2)
        SelectorFeedback(db_session).record(
            selector, ExtractionScore(confidence=0.95, jobs_found=10)
        )
        assert selector.consecutive_failures == 0
        assert selector.success_count == 1

    def test_failure_decays_confidence(self, db_session: Session) -> None:
        selector = self._selector(db_session)
        SelectorFeedback(db_session).record(
            selector, ExtractionScore(confidence=0.0, jobs_found=0)
        )
        assert selector.confidence_score < 0.9
        assert selector.consecutive_failures == 1

    def test_one_off_failure_does_not_trigger_regeneration(
        self, db_session: Session
    ) -> None:
        """Rate-based triggers relearn healthy sites; consecutive ones do not."""
        selector = self._selector(db_session, consecutive_failures=1)
        assert SelectorFeedback(db_session).should_regenerate(selector) is False

    def test_sustained_failure_triggers_regeneration(self, db_session: Session) -> None:
        selector = self._selector(db_session, consecutive_failures=3)
        assert SelectorFeedback(db_session).should_regenerate(selector) is True

    def test_absent_selector_always_triggers_learning(self, db_session: Session) -> None:
        assert SelectorFeedback(db_session).should_regenerate(None) is True

    def test_degraded_websites_are_listed(self, db_session: Session) -> None:
        self._selector(db_session, is_active=True)
        db_session.flush()
        selector = SelectorRepository(db_session).get_active(WEBSITE)
        assert selector is not None
        selector.confidence_score = 0.2
        db_session.flush()

        assert WEBSITE in SelectorFeedback(db_session).degraded_websites()
