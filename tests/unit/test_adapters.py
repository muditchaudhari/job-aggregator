"""Adapter and detection tests, driven entirely by canned payloads."""

from __future__ import annotations

from typing import Any, cast

import pytest

from app.models.enums import ATSType, ExtractionTier, ScrapingStrategy
from app.scrapers.adapters import greenhouse as greenhouse_module
from app.scrapers.adapters import lever as lever_module
from app.scrapers.adapters.ashby import AshbyScraper
from app.scrapers.adapters.generic_html import GenericHtmlScraper
from app.scrapers.adapters.greenhouse import GreenhouseScraper
from app.scrapers.adapters.lever import LeverScraper
from app.scrapers.adapters.smartrecruiters import SmartRecruitersScraper
from app.scrapers.adapters.workday import WorkdayScraper
from app.scrapers.detection import detect, detect_from_hostname
from app.scrapers.fetcher import FetchResult
from app.scrapers.registry import ADAPTERS, get_adapter
from tests.fixtures.fakes import FakeFetcher
from tests.fixtures.pages import (
    GENERIC_LISTING_HTML,
    GREENHOUSE_API,
    LEVER_API,
    NO_JOBS_HTML,
    SPA_SHELL_HTML,
    WORKDAY_CXS,
)


class TestTokenExtraction:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://boards.greenhouse.io/acme", "acme"),
            ("https://job-boards.greenhouse.io/acme", "acme"),
            ("https://boards.greenhouse.io/embed/job_board?for=acme", "acme"),
        ],
    )
    def test_greenhouse_from_url(self, url: str, expected: str) -> None:
        assert greenhouse_module.extract_board_token(url) == expected

    def test_greenhouse_from_embedded_script(self) -> None:
        """An embedded board's token only appears in the page source."""
        body = '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>'
        assert greenhouse_module.extract_board_token("https://acme.com/careers", body) == "acme"

    def test_lever_from_url(self) -> None:
        assert lever_module.extract_board_token("https://jobs.lever.co/acme") == "acme"


class TestGreenhouse:
    def test_extracts_from_api_payload(self, company: Any) -> None:
        fetcher = FakeFetcher(
            json_pages={"https://boards-api.greenhouse.io/v1/boards/acme/jobs": GREENHOUSE_API}
        )
        scraper = GreenhouseScraper(company, cast(Any, fetcher))
        outcome = scraper.scrape()

        assert outcome.tier is ExtractionTier.API
        assert outcome.confidence == 1.0
        assert len(outcome.jobs) == 2

        job = outcome.jobs[0]
        assert job.title == "Senior Backend Engineer"
        assert job.external_id == "4012345"
        assert job.location == "Bengaluru, India"
        assert job.department == "Engineering"
        assert job.employment_type == "Full-time"
        # Content arrives HTML-escaped; tags must be gone from the result.
        assert "Python" in (job.description or "")
        assert "<strong>" not in (job.description or "")


class TestLever:
    def test_extracts_from_api_payload(self, company: Any) -> None:
        company.ats_type = ATSType.LEVER
        company.career_url = "https://jobs.lever.co/acme"
        company.board_token = "acme"

        fetcher = FakeFetcher(
            json_pages={"https://api.lever.co/v0/postings/acme": LEVER_API}
        )
        outcome = LeverScraper(company, cast(Any, fetcher)).scrape()

        job = outcome.jobs[0]
        assert job.title == "Staff Software Engineer"
        assert job.remote == "hybrid"
        assert job.requirements is not None and "5+ years" in job.requirements
        # Epoch milliseconds pass through as a string for normalisation.
        assert job.posted_at == "1753000000000"


class TestWorkday:
    def test_reconstructs_the_cxs_endpoint(self, company: Any) -> None:
        company.ats_type = ATSType.WORKDAY
        company.career_url = "https://acme.wd1.myworkdayjobs.com/en-US/External"

        endpoint = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"
        fetcher = FakeFetcher(json_pages={endpoint: WORKDAY_CXS})
        outcome = WorkdayScraper(company, cast(Any, fetcher)).scrape()

        assert fetcher.requested[0][0] == endpoint
        assert len(outcome.jobs) == 2
        assert outcome.jobs[0].external_id == "R-12345"
        assert outcome.jobs[0].url.endswith("Software-Engineer-II_R-12345")
        assert outcome.jobs[0].posted_at == "Posted 3 Days Ago"

    def test_rejects_a_non_workday_host(self, company: Any) -> None:
        from app.core.errors import PermanentFetchError

        company.career_url = "https://acme.com/careers"
        with pytest.raises(PermanentFetchError):
            WorkdayScraper(company, cast(Any, FakeFetcher())).fetch()


class TestSmartRecruitersAndAshby:
    def test_smartrecruiters_builds_posting_urls(self, company: Any) -> None:
        company.career_url = "https://jobs.smartrecruiters.com/Acme"
        company.board_token = "Acme"
        payload = {
            "totalFound": 1,
            "content": [
                {
                    "id": "744000",
                    "name": "QA Engineer",
                    "location": {"city": "Pune", "country": "India", "remote": False},
                    "releasedDate": "2026-07-25T00:00:00.000Z",
                    "typeOfEmployment": {"label": "Full-time"},
                }
            ],
        }
        fetcher = FakeFetcher(
            json_pages={"https://api.smartrecruiters.com/v1/companies/Acme/postings": payload}
        )
        outcome = SmartRecruitersScraper(company, cast(Any, fetcher)).scrape()
        assert outcome.jobs[0].url == "https://jobs.smartrecruiters.com/Acme/744000"
        assert outcome.jobs[0].location == "Pune, India"

    def test_ashby_skips_unlisted_postings(self, company: Any) -> None:
        company.career_url = "https://jobs.ashbyhq.com/acme"
        company.board_token = "acme"
        payload = {
            "jobs": [
                {"id": "1", "title": "Listed", "jobUrl": "https://x.com/1", "isListed": True},
                {"id": "2", "title": "Hidden", "jobUrl": "https://x.com/2", "isListed": False},
            ]
        }
        fetcher = FakeFetcher(
            json_pages={"https://api.ashbyhq.com/posting-api/job-board/acme": payload}
        )
        outcome = AshbyScraper(company, cast(Any, fetcher)).scrape()
        assert [job.title for job in outcome.jobs] == ["Listed"]


class TestGenericHtml:
    def test_builtin_selectors_handle_a_conventional_listing(
        self, generic_company: Any
    ) -> None:
        fetcher = FakeFetcher(pages={generic_company.career_url: GENERIC_LISTING_HTML})
        outcome = GenericHtmlScraper(generic_company, cast(Any, fetcher)).scrape()

        assert len(outcome.jobs) == 3
        assert outcome.confidence > 0.5

    def test_page_with_no_openings_yields_nothing_usable(
        self, generic_company: Any
    ) -> None:
        fetcher = FakeFetcher(pages={generic_company.career_url: NO_JOBS_HTML})
        outcome = GenericHtmlScraper(generic_company, cast(Any, fetcher)).scrape()
        assert outcome.confidence < 0.6


class TestDetection:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://boards.greenhouse.io/acme", ATSType.GREENHOUSE),
            ("https://jobs.lever.co/acme", ATSType.LEVER),
            ("https://jobs.ashbyhq.com/acme", ATSType.ASHBY),
            ("https://acme.wd1.myworkdayjobs.com/External", ATSType.WORKDAY),
            ("https://jobs.smartrecruiters.com/Acme", ATSType.SMARTRECRUITERS),
            ("https://acme.taleo.net/careersection/x/", ATSType.TALEO),
        ],
    )
    def test_hostname_detection_needs_no_request(
        self, url: str, expected: ATSType
    ) -> None:
        result = detect_from_hostname(url)
        assert result is not None
        assert result.ats_type is expected
        assert result.is_confident

    def test_hostname_detection_extracts_the_token(self) -> None:
        from app.scrapers.detection import detect as detect_full

        fetcher = FakeFetcher()
        result = detect_full("https://boards.greenhouse.io/acme", cast(Any, fetcher))
        assert result.board_token == "acme"
        # Hostname detection must not have issued any request.
        assert fetcher.requested == []

    def test_embedded_board_detected_from_body(self) -> None:
        body = (
            '<html><body><script src="https://boards.greenhouse.io/embed/job_board/js'
            '?for=acme"></script></body></html>'
        )
        fetcher = FakeFetcher(pages={"https://acme.com/careers": body})
        result = detect("https://acme.com/careers", cast(Any, fetcher))
        assert result.ats_type is ATSType.GREENHOUSE
        assert result.board_token == "acme"

    def test_spa_shell_falls_back_to_custom_react(self) -> None:
        fetcher = FakeFetcher(pages={"https://acme.com/careers": SPA_SHELL_HTML})
        result = detect("https://acme.com/careers", cast(Any, fetcher))
        assert result.ats_type is ATSType.CUSTOM_REACT
        assert result.strategy is ScrapingStrategy.PLAYWRIGHT

    def test_plain_page_is_generic_html(self) -> None:
        fetcher = FakeFetcher(pages={"https://acme.com/careers": GENERIC_LISTING_HTML})
        result = detect("https://acme.com/careers", cast(Any, fetcher))
        assert result.ats_type is ATSType.GENERIC_HTML

    def test_unfetchable_page_degrades_rather_than_raising(self) -> None:
        result = detect("https://acme.com/careers", cast(Any, FakeFetcher()))
        assert result.ats_type is ATSType.GENERIC_HTML
        assert not result.is_confident


class TestRegistry:
    def test_every_ats_type_resolves(self) -> None:
        for adapter in ADAPTERS:
            assert get_adapter(adapter.ats_type) is adapter

    def test_unknown_falls_back_to_generic(self) -> None:
        assert get_adapter(ATSType.UNKNOWN) is GenericHtmlScraper

    def test_adapters_agree_on_the_raw_job_contract(self, company: Any) -> None:
        """The point of the base class: one shape out, whatever went in."""
        fetcher = FakeFetcher(
            json_pages={"https://boards-api.greenhouse.io/v1/boards/acme/jobs": GREENHOUSE_API},
            pages={company.career_url: GENERIC_LISTING_HTML},
        )
        api_jobs = GreenhouseScraper(company, cast(Any, fetcher)).scrape().jobs
        html_jobs = GenericHtmlScraper(
            company,
            cast(Any, fetcher),
        ).extract_jobs(
            FetchResult(
                url=company.career_url,
                final_url=company.career_url,
                status_code=200,
                text=GENERIC_LISTING_HTML,
                content_type="text/html",
                strategy=ScrapingStrategy.HTTP,
                fetch_ms=1,
            )
        )
        for job in [*api_jobs, *html_jobs]:
            assert job.is_usable()
            assert isinstance(job.title, str)
            assert isinstance(job.raw, dict)


class TestPhenom:
    """Adobe-style Phenom People sites."""

    DDO = """
    <html><body><script>
    phApp.ddo = {"eagerLoadRefineSearch":{"totalHits":2,"data":{"jobs":[
      {"title":"Member of Technical Staff II","jobId":"R166197",
       "applyUrl":"https://adobe.wd5.myworkdayjobs.com/x/job/1",
       "location":"Bangalore, India","category":"Engineering","type":"Full time",
       "postedDate":"2026-07-05T00:00:00.000+0000",
       "descriptionTeaser":"Build <b>things</b> with a } brace in the text"},
      {"title":"Computer Scientist","jobId":"R166198",
       "applyUrl":"https://adobe.wd5.myworkdayjobs.com/x/job/2",
       "location":"Noida, India","category":"Engineering","type":"Full time"}
    ]}}};
    </script></body></html>
    """

    def test_extracts_from_the_embedded_ddo(self, generic_company: Any) -> None:
        from app.scrapers.adapters.phenom import PhenomScraper

        generic_company.career_url = "https://careers.adobe.com/us/en/search-results"
        fetcher = FakeFetcher(pages={generic_company.career_url: self.DDO})
        outcome = PhenomScraper(generic_company, cast(Any, fetcher)).scrape()

        assert len(outcome.jobs) == 2
        assert outcome.tier is ExtractionTier.EMBEDDED_JSON
        job = outcome.jobs[0]
        assert job.title == "Member of Technical Staff II"
        assert job.external_id == "R166197"
        assert job.location == "Bangalore, India"
        assert "<b>" not in (job.description or "")

    def test_brace_inside_a_string_does_not_truncate_the_blob(self) -> None:
        """A lazy regex would stop at the '}' inside descriptionTeaser."""
        from app.scrapers.adapters.phenom import extract_ddo

        ddo = extract_ddo(self.DDO)
        assert ddo is not None
        assert ddo["eagerLoadRefineSearch"]["data"]["jobs"][0]["jobId"] == "R166197"

    def test_a_filtered_url_is_not_paginated(self, generic_company: Any) -> None:
        """Phenom drops the filter on page 2+, so paginating a search would
        replace 10 wanted results with the whole unfiltered board."""
        from app.scrapers.adapters.phenom import PhenomScraper, has_search_filter

        assert has_search_filter("https://careers.adobe.com/us/en/search-results?keywords=mts")
        assert not has_search_filter("https://careers.adobe.com/us/en/search-results")

        url = "https://careers.adobe.com/us/en/search-results?keywords=mts"
        generic_company.career_url = url
        # No canned widget response, so the adapter falls back to the rendered
        # pages — which is exactly the path the single-page guard protects.
        fetcher = FakeFetcher(pages={url: self.DDO})
        scraper = PhenomScraper(generic_company, cast(Any, fetcher))
        scraper.scrape()

        assert scraper._used_widget is False
        page_gets = [r for r in fetcher.requested if r[1] is not ScrapingStrategy.API]
        assert len(page_gets) == 1, "a filtered URL must not be paginated"

    def test_widget_api_is_preferred_and_paginates(self, generic_company: Any) -> None:
        """The widget endpoint takes size=100 and keeps the filter on page 2+."""
        from app.scrapers.adapters.phenom import PhenomScraper

        url = "https://careers.adobe.com/us/en/search-results"
        generic_company.career_url = url
        widget = {
            "refineSearch": {
                "totalHits": 2,
                "data": {"jobs": [
                    {"title": "MTS II", "jobId": "R1", "applyUrl": "https://x.com/1",
                     "location": "Bangalore, India"},
                    {"title": "MTS I", "jobId": "R2", "applyUrl": "https://x.com/2",
                     "location": "Noida, India"},
                ]},
            }
        }
        fetcher = FakeFetcher(
            pages={url: self.DDO},
            json_pages={"https://careers.adobe.com/widgets": widget},
        )
        scraper = PhenomScraper(generic_company, cast(Any, fetcher))
        outcome = scraper.scrape()

        assert scraper._used_widget is True
        assert outcome.tier is ExtractionTier.API
        assert [j.title for j in outcome.jobs] == ["MTS II", "MTS I"]

    def test_workday_marker_does_not_hijack_a_phenom_page(self) -> None:
        """Phenom apply links point at Workday; that must not misroute detection."""
        from app.scrapers.detection import detect_from_body

        result = detect_from_body(self.DDO, "https://careers.adobe.com/us/en/search-results")
        assert result is not None
        assert result.ats_type is ATSType.PHENOM
