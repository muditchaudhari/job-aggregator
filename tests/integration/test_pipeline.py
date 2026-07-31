"""Extraction ladder and end-to-end scan.

These are the tests that prove the cost model actually holds: that a healthy
site never reaches the LLM, that an unknown one learns a strategy once, and
that the *second* scan of that site is free.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from sqlalchemy.orm import Session

from app.extractors.pipeline import ExtractionPipeline
from app.llm.client import LLMClient
from app.models.enums import ExtractionTier, ScrapeStatus
from app.repositories.selector import SelectorRepository
from app.services.scan import ScanService
from tests.fixtures.fakes import (
    FakeBudgetTracker,
    FakeFetcher,
    FakeLLMProvider,
    RecordingSender,
)
from tests.fixtures.pages import (
    GENERIC_LISTING_HTML,
    GREENHOUSE_API,
    JSON_LD_HTML,
    LLM_SELECTOR_RESPONSE,
    NO_JOBS_HTML,
)


def llm_client(responses: list[str]) -> LLMClient:
    return LLMClient(
        provider=FakeLLMProvider(responses), budget=cast(Any, FakeBudgetTracker())
    )


class TestLadderOrdering:
    def test_api_backed_company_never_touches_html(
        self, db_session: Session, company: Any
    ) -> None:
        fetcher = FakeFetcher(
            json_pages={"https://boards-api.greenhouse.io/v1/boards/acme/jobs": GREENHOUSE_API}
        )
        provider = FakeLLMProvider([LLM_SELECTOR_RESPONSE])
        pipeline = ExtractionPipeline(
            db_session,
            cast(Any, fetcher),
            llm_client=LLMClient(provider=provider, budget=cast(Any, FakeBudgetTracker())),
        )

        result = pipeline.run(company)

        assert result.tier is ExtractionTier.API
        assert len(result.jobs) == 2
        assert provider.calls == []
        assert result.usage.calls == 0

    def test_embedded_json_beats_selectors(
        self, db_session: Session, generic_company: Any
    ) -> None:
        fetcher = FakeFetcher(pages={generic_company.career_url: JSON_LD_HTML})
        provider = FakeLLMProvider([LLM_SELECTOR_RESPONSE])
        pipeline = ExtractionPipeline(
            db_session,
            cast(Any, fetcher),
            llm_client=LLMClient(provider=provider, budget=cast(Any, FakeBudgetTracker())),
        )

        result = pipeline.run(generic_company)

        assert result.tier is ExtractionTier.EMBEDDED_JSON
        assert provider.calls == []

    def test_builtin_selectors_handle_a_conventional_page(
        self, db_session: Session, generic_company: Any
    ) -> None:
        fetcher = FakeFetcher(pages={generic_company.career_url: GENERIC_LISTING_HTML})
        provider = FakeLLMProvider([LLM_SELECTOR_RESPONSE])
        pipeline = ExtractionPipeline(
            db_session,
            cast(Any, fetcher),
            llm_client=LLMClient(provider=provider, budget=cast(Any, FakeBudgetTracker())),
        )

        result = pipeline.run(generic_company)

        assert result.tier is ExtractionTier.CSS_SELECTOR
        assert len(result.jobs) == 3
        assert provider.calls == []


class TestSelfLearning:
    #: Markup no built-in selector recognises, so the ladder must reach tier 5.
    UNUSUAL_HTML = """
    <html><body><main>
      <div class="pos-row" data-testid="posting">
        <span class="pos-name">Platform Engineer</span>
        <a href="/openings/101">Details</a>
        <span class="pos-where">Bengaluru</span>
      </div>
      <div class="pos-row" data-testid="posting">
        <span class="pos-name">Data Engineer</span>
        <a href="/openings/102">Details</a>
        <span class="pos-where">Remote</span>
      </div>
      <div class="pos-row" data-testid="posting">
        <span class="pos-name">Site Reliability Engineer</span>
        <a href="/openings/103">Details</a>
        <span class="pos-where">Pune</span>
      </div>
    </main></body></html>
    """

    RESPONSE = """{
      "container_selector": "div.pos-row",
      "title_selector": "span.pos-name",
      "url_selector": "a",
      "location_selector": "span.pos-where",
      "date_selector": null,
      "description_selector": null,
      "requires_render": false,
      "confidence": 0.9,
      "notes": "Repeating div rows."
    }"""

    def test_unknown_page_is_learned_once(
        self, db_session: Session, generic_company: Any
    ) -> None:
        fetcher = FakeFetcher(pages={generic_company.career_url: self.UNUSUAL_HTML})
        provider = FakeLLMProvider([self.RESPONSE])
        pipeline = ExtractionPipeline(
            db_session,
            cast(Any, fetcher),
            llm_client=LLMClient(provider=provider, budget=cast(Any, FakeBudgetTracker())),
        )

        result = pipeline.run(generic_company)

        assert result.tier is ExtractionTier.LLM
        assert len(result.jobs) == 3
        assert len(provider.calls) == 1

        stored = SelectorRepository(db_session).get_active(generic_company.website)
        assert stored is not None
        assert stored.container_selector == "div.pos-row"

    def test_second_scan_of_a_learned_site_is_free(
        self, db_session: Session, generic_company: Any
    ) -> None:
        """The whole point of learning: pay once, reuse forever."""
        fetcher = FakeFetcher(pages={generic_company.career_url: self.UNUSUAL_HTML})
        provider = FakeLLMProvider([self.RESPONSE])
        client = LLMClient(provider=provider, budget=cast(Any, FakeBudgetTracker()))

        ExtractionPipeline(db_session, cast(Any, fetcher), llm_client=client).run(
            generic_company
        )
        db_session.commit()

        second = ExtractionPipeline(
            db_session, cast(Any, fetcher), llm_client=client
        ).run(generic_company)

        assert second.tier is ExtractionTier.CSS_SELECTOR
        assert second.selector_version == 1
        assert len(second.jobs) == 3
        # Still exactly one call in total.
        assert len(provider.calls) == 1

    def test_llm_prompt_receives_reduced_html_only(
        self, db_session: Session, generic_company: Any
    ) -> None:
        """Never send whole documents (brief: 'LLM Usage')."""
        bulky = self.UNUSUAL_HTML + "<script>" + ("x" * 200_000) + "</script>"
        fetcher = FakeFetcher(pages={generic_company.career_url: bulky})
        provider = FakeLLMProvider([self.RESPONSE])

        ExtractionPipeline(
            db_session,
            cast(Any, fetcher),
            llm_client=LLMClient(provider=provider, budget=cast(Any, FakeBudgetTracker())),
        ).run(generic_company)

        prompt = provider.calls[0]["prompt"]
        assert len(prompt) < 30_000
        assert "xxxxxxxxxx" not in prompt
        assert "pos-row" in prompt

    def test_budget_exhaustion_disables_tier_five_without_crashing(
        self, db_session: Session, generic_company: Any
    ) -> None:
        fetcher = FakeFetcher(pages={generic_company.career_url: self.UNUSUAL_HTML})
        client = LLMClient(
            provider=FakeLLMProvider([self.RESPONSE]),
            budget=cast(Any, FakeBudgetTracker(exhausted=True)),
        )

        result = ExtractionPipeline(
            db_session, cast(Any, fetcher), llm_client=client
        ).run(generic_company)

        assert result.jobs == []
        assert "llm:budget_exceeded" in result.trail

    def test_llm_disabled_leaves_tiers_one_to_four_working(
        self, db_session: Session, generic_company: Any
    ) -> None:
        from app.llm.providers import NullProvider

        fetcher = FakeFetcher(pages={generic_company.career_url: GENERIC_LISTING_HTML})
        client = LLMClient(
            provider=NullProvider(), budget=cast(Any, FakeBudgetTracker())
        )

        result = ExtractionPipeline(
            db_session, cast(Any, fetcher), llm_client=client
        ).run(generic_company)

        assert result.succeeded
        assert result.tier is ExtractionTier.CSS_SELECTOR


class TestScanEndToEnd:
    def _service(
        self, db_session: Session, fetcher: FakeFetcher, sender: RecordingSender
    ) -> ScanService:
        """Scan service wired to a recording channel instead of real SMTP."""
        from app.models.enums import NotificationChannel
        from app.notifications.dispatcher import NotificationDispatcher

        return ScanService(
            db_session,
            fetcher=cast(Any, fetcher),
            dispatcher=NotificationDispatcher(
                db_session, senders={NotificationChannel.EMAIL: cast(Any, sender)}
            ),
        )

    def test_first_scan_is_a_silent_baseline(
        self, db_session: Session, company: Any, profile: Any
    ) -> None:
        """Registering a company must not deliver its entire existing board."""
        fetcher = FakeFetcher(
            json_pages={"https://boards-api.greenhouse.io/v1/boards/acme/jobs": GREENHOUSE_API}
        )
        sender = RecordingSender("email")
        report = self._service(db_session, fetcher, sender).scan_company(company.id)

        assert report.baseline is True
        assert report.jobs_new == 2
        assert sender.sent == []

        # Scores are still computed, so /jobs/new is useful straight away.
        from app.repositories.job import JobRepository
        from app.repositories.match import JobMatchRepository

        jobs, _ = JobRepository(db_session).list_filtered(company_id=company.id)
        scores = JobMatchRepository(db_session).scores_for_jobs(
            [j.id for j in jobs], profile.id
        )
        assert len(scores) == 2

    def test_postings_added_after_the_baseline_do_notify(
        self, db_session: Session, company: Any, profile: Any
    ) -> None:
        base_url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
        fetcher = FakeFetcher(json_pages={base_url: GREENHOUSE_API})
        sender = RecordingSender("email")
        service = self._service(db_session, fetcher, sender)

        service.scan_company(company.id)
        assert sender.sent == []

        # The board gains a posting.
        grown = {
            "jobs": [
                *GREENHOUSE_API["jobs"],
                {
                    "id": 4012347,
                    "title": "Backend Engineer",
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/4012347",
                    "location": {"name": "Bengaluru, India"},
                    "updated_at": "2026-07-30T09:00:00-04:00",
                    "content": "&lt;p&gt;Python, AWS, Docker, SQL.&lt;/p&gt;",
                    "departments": [{"id": 1, "name": "Engineering"}],
                    "metadata": [],
                },
            ]
        }
        fetcher.json_pages[base_url] = grown
        second = service.scan_company(company.id)

        assert second.baseline is False
        assert second.jobs_new == 1
        # Only the genuinely new posting was delivered.
        assert len(sender.sent) == 1
        assert sender.sent[0][1].job_title == "Backend Engineer"

    def test_full_pipeline_produces_jobs_matches_and_notifications(
        self, db_session: Session, company: Any, profile: Any
    ) -> None:
        fetcher = FakeFetcher(
            json_pages={"https://boards-api.greenhouse.io/v1/boards/acme/jobs": GREENHOUSE_API}
        )
        sender = RecordingSender("email")
        report = self._service(db_session, fetcher, sender).scan_company(company.id)

        assert report.status is ScrapeStatus.SUCCESS
        assert report.jobs_found == 2
        assert report.jobs_new == 2
        assert report.extraction_tier == str(ExtractionTier.API)

        from app.repositories.job import JobRepository

        jobs, total = JobRepository(db_session).list_filtered(company_id=company.id)
        assert total == 2
        # Normalisation ran: the escaped HTML description is plain text now.
        senior = next(job for job in jobs if "Senior" in job.title)
        assert senior.description and "<strong>" not in senior.description
        assert senior.location_city == "Bengaluru"

    def test_rescan_finds_nothing_new(
        self, db_session: Session, company: Any, profile: Any
    ) -> None:
        """Deduplication working end to end — no repeat notifications."""
        fetcher = FakeFetcher(
            json_pages={"https://boards-api.greenhouse.io/v1/boards/acme/jobs": GREENHOUSE_API}
        )
        sender = RecordingSender("email")
        service = self._service(db_session, fetcher, sender)

        service.scan_company(company.id)
        first_sends = len(sender.sent)
        second = service.scan_company(company.id)

        assert second.jobs_found == 2
        assert second.jobs_new == 0
        assert second.duplicates == 2
        assert len(sender.sent) == first_sends

    def test_failed_scan_records_a_run_and_backs_off(
        self, db_session: Session, generic_company: Any
    ) -> None:
        fetcher = FakeFetcher(pages={generic_company.career_url: NO_JOBS_HTML})
        service = ScanService(db_session, fetcher=cast(Any, fetcher))

        report = service.scan_company(generic_company.id)

        assert report.status is ScrapeStatus.FAILED
        assert generic_company.consecutive_failures == 1
        assert generic_company.next_scrape_at is not None

        from app.repositories.scrape_run import ScrapeRunRepository

        run = ScrapeRunRepository(db_session).latest_for_company(generic_company.id)
        assert run is not None
        assert run.status is ScrapeStatus.FAILED
        assert run.error

    def test_unreachable_page_does_not_raise(
        self, db_session: Session, generic_company: Any
    ) -> None:
        service = ScanService(db_session, fetcher=cast(Any, FakeFetcher()))
        report = service.scan_company(generic_company.id)
        assert report.status is ScrapeStatus.FAILED

    def test_missing_company_raises_not_found(self, db_session: Session) -> None:
        import uuid

        from app.core.errors import NotFoundError

        service = ScanService(db_session, fetcher=cast(Any, FakeFetcher()))
        with pytest.raises(NotFoundError):
            service.scan_company(uuid.uuid4())
