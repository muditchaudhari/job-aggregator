"""Oracle Fusion Recruiting Cloud adapter."""

from __future__ import annotations

import pytest

from app.core.errors import PermanentFetchError
from app.models.company import Company
from app.models.enums import ATSType, ScrapingStrategy
from app.scrapers.adapters.oracle import OracleScraper
from app.scrapers.fetcher import FetchResult

JOBS_URL = (
    "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/jobs"
    "?location=Bengaluru%2C+Karnataka%2C+India&locationId=300000081145775"
    "&locationLevel=city&mode=location&radius=25&radiusUnit=MI"
)


def _scraper(career_url: str = JOBS_URL) -> OracleScraper:
    company = Company(
        name="JPMorgan Chase",
        career_url=career_url,
        website="jpmorganchase.com",
        ats_type=ATSType.ORACLE,
    )
    return OracleScraper(company, None)


def _result(entries: list[dict]) -> FetchResult:
    return FetchResult(
        url="x", final_url="x", status_code=200, text="",
        content_type="application/json",
        strategy=ScrapingStrategy.API, fetch_ms=0,
        json_body={"requisitionList": entries, "total": len(entries)},
    )


class TestEndpointReconstruction:
    def test_site_id_comes_from_the_path(self) -> None:
        assert _scraper()._parts() == ("https://jpmc.fa.oraclecloud.com", "CX_1001")

    def test_a_city_filter_is_translated_to_the_api_name(self) -> None:
        """The site calls it locationId; the API calls it selectedLocationsFacet.

        Without the translation the query pulls the tenant's entire global
        board — thousands of postings where the URL asked for one city.
        """
        parts = _scraper()._finder_parts()

        assert parts == [
            "siteNumber=CX_1001",
            "selectedLocationsFacet=300000081145775",
        ]

    def test_an_unfiltered_url_scopes_to_the_site_only(self) -> None:
        unfiltered = (
            "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/jobs"
        )
        assert _scraper(unfiltered)._finder_parts() == ["siteNumber=CX_1001"]

    def test_the_finder_is_url_encoded_into_one_parameter(self) -> None:
        url = _scraper()._requisitions_url(["limit=1"])

        assert "finder=findReqs%3BsiteNumber%3DCX_1001" in url
        assert url.startswith(
            "https://jpmc.fa.oraclecloud.com/hcmRestApi/resources/latest"
            "/recruitingCEJobRequisitions"
        )

    def test_a_non_oracle_host_is_rejected(self) -> None:
        with pytest.raises(PermanentFetchError, match="not a recognisable Oracle"):
            _scraper("https://boards.greenhouse.io/adyen")._parts()

    def test_a_url_without_a_site_id_is_rejected(self) -> None:
        with pytest.raises(PermanentFetchError, match="no site id"):
            _scraper("https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en")._parts()


class TestExtraction:
    def test_the_deep_link_is_built_from_the_id(self) -> None:
        """The list response carries no URL field, so the link is constructed.

        Verified against the live site: this is the page that renders the job
        with an apply button, not an API record.
        """
        jobs = _scraper().extract_jobs(_result([{
            "Id": 210754767,
            "Title": "Java Full Stack Software Engineer II",
            "PrimaryLocation": "Bengaluru, Karnataka, India",
            "PostedDate": "2026-11-03",
            "JobFamily": "Software Engineering",
            "JobSchedule": "Full time",
            "ShortDescriptionStr": "<p>Build things.</p>",
        }]))

        assert jobs[0].url == (
            "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience"
            "/en/sites/CX_1001/job/210754767"
        )
        assert jobs[0].external_id == "210754767"
        assert jobs[0].location == "Bengaluru, Karnataka, India"
        assert jobs[0].posted_at == "2026-11-03"
        assert jobs[0].description == "Build things."

    def test_a_posting_without_an_id_gets_no_url(self) -> None:
        jobs = _scraper().extract_jobs(_result([{"Title": "Mystery role"}]))

        assert jobs[0].url is None

    def test_an_empty_board_extracts_nothing(self) -> None:
        assert _scraper().extract_jobs(_result([])) == []


class TestDetection:
    def test_the_host_identifies_the_platform(self) -> None:
        from app.scrapers.detection import detect_from_hostname

        result = detect_from_hostname(JOBS_URL)

        assert result is not None
        assert result.ats_type is ATSType.ORACLE
        assert result.strategy is ScrapingStrategy.API

    def test_the_registry_resolves_the_adapter(self) -> None:
        from app.scrapers.registry import api_backed_types, get_adapter

        assert get_adapter(ATSType.ORACLE) is OracleScraper
        assert ATSType.ORACLE in api_backed_types()
