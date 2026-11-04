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

    def test_an_unrelated_board_is_rejected(self) -> None:
        """Rejected on the missing site id, not the host.

        A non-Oracle host is no longer disqualifying by itself — it may be a
        white-labelled tenant — so the site id is what identifies the platform
        in the URL. See TestWhiteLabelledTenants.
        """
        with pytest.raises(PermanentFetchError, match="no site id"):
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


VANITY_URL = (
    "https://careers.americanexpress.com/en/sites/CX_1/jobs"
    "?location=Bengaluru%2C+KA%2C+India&locationId=300000032718926"
    "&locationLevel=city&mode=location&radius=25&radiusUnit=MI"
)


class TestWhiteLabelledTenants:
    """A tenant behind the company's own domain broke two assumptions.

    The host regex required ``<tenant>.fa.oraclecloud.com``, but a regional
    tenant is ``egug.fa.us2.oraclecloud.com``. And a vanity domain serves the
    UI without the REST API: asking it for the API earns a redirect to the
    site's 404 page, which answers 200 — so neither the API nor the job link
    can be checked by status code alone.
    """

    def test_a_regional_oracle_host_is_accepted(self) -> None:
        from app.scrapers.adapters.oracle import _HOST_RE

        assert _HOST_RE.match("egug.fa.us2.oraclecloud.com")
        assert _HOST_RE.match("jpmc.fa.oraclecloud.com")
        assert not _HOST_RE.match("careers.americanexpress.com")

    def test_the_tenant_is_read_from_the_vanity_page(self) -> None:
        """The real host is in that page's HTML, because it loads assets from it."""
        company = Company(
            name="Amex", career_url=VANITY_URL, website="americanexpress.com",
            ats_type=ATSType.ORACLE,
        )
        scraper = OracleScraper(company, _FakeFetcher(
            '<link href="https://egug.fa.us2.oraclecloud.com/hcmUI/CandExpStatic/x.css">'
        ))

        assert scraper._parts() == ("https://egug.fa.us2.oraclecloud.com", "CX_1")
        # Cached, so the extra page fetch happens once rather than every run.
        assert company.api_endpoint == "https://egug.fa.us2.oraclecloud.com"

    def test_a_vanity_job_link_omits_the_hcmui_prefix(self) -> None:
        """The branded site serves /<locale>/sites/<site>/job/<id>.

        Verified in a browser: the prefixed path redirects to the vanity 404.
        """
        company = Company(
            name="Amex", career_url=VANITY_URL, website="americanexpress.com",
            ats_type=ATSType.ORACLE, api_endpoint="https://egug.fa.us2.oraclecloud.com",
        )
        jobs = OracleScraper(company, None).extract_jobs(_result_for(
            {"Id": 26014373, "Title": "Financial Systems Analyst II"}
        ))

        assert jobs[0].url == (
            "https://careers.americanexpress.com/en/sites/CX_1/job/26014373"
        )
        assert "hcmUI" not in jobs[0].url

    def test_an_oracle_hosted_link_keeps_the_hcmui_prefix(self) -> None:
        company = Company(
            name="JPMC", career_url=JOBS_URL, website="jpmorganchase.com",
            ats_type=ATSType.ORACLE,
        )
        jobs = OracleScraper(company, None).extract_jobs(_result_for(
            {"Id": 1, "Title": "X"}
        ))

        assert jobs[0].url == (
            "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience"
            "/en/sites/CX_1001/job/1"
        )

    def test_a_domain_with_no_tenant_behind_it_is_rejected(self) -> None:
        company = Company(
            name="Nope", career_url="https://careers.example.com/en/sites/CX_1/jobs",
            website="example.com", ats_type=ATSType.ORACLE,
        )
        scraper = OracleScraper(company, _FakeFetcher("<html>no tenant here</html>"))

        with pytest.raises(PermanentFetchError, match="no Oracle Fusion tenant"):
            scraper._parts()


class _FakeFetcher:
    def __init__(self, body: str) -> None:
        self._body = body

    def fetch(self, url: str, **_kw):
        return FetchResult(
            url=url, final_url=url, status_code=200, text=self._body,
            content_type="text/html", strategy=ScrapingStrategy.HTTP, fetch_ms=1,
        )


def _result_for(entry: dict) -> FetchResult:
    return FetchResult(
        url="x", final_url="x", status_code=200, text="",
        content_type="application/json", strategy=ScrapingStrategy.API, fetch_ms=0,
        json_body={"requisitionList": [entry], "total": 1},
    )
