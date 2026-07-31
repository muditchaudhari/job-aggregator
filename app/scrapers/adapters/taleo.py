"""Oracle Taleo.

Taleo career sections are frame-heavy, session-bound, and among the least
pleasant boards to read. Two things make it tractable:

* the listing itself is a plain table once rendered, and
* many tenants expose a ``requisition`` REST endpoint that returns JSON.

The REST endpoint is attempted first and the table is the fallback.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar
from urllib.parse import urljoin, urlparse

from app.core.logging import get_logger
from app.extractors.selector_extractor import SelectorSet
from app.models.enums import ATSType, ExtractionTier, ScrapingStrategy
from app.scrapers.adapters.html_base import HtmlListingScraper
from app.scrapers.base import RawJob
from app.scrapers.fetcher import FetchResult
from app.utils.text import clean_text

logger = get_logger(__name__)

_CAREER_SECTION_RE = re.compile(r"/careersection/([\w.-]+)/", re.IGNORECASE)


class TaleoScraper(HtmlListingScraper):
    ats_type: ClassVar[ATSType] = ATSType.TALEO
    #: AUTO rather than HTTP: some tenants server-render the table and some
    #: build it from a second XHR, and which one you get varies by theme.
    default_strategy: ClassVar[ScrapingStrategy] = ScrapingStrategy.AUTO
    host_markers: ClassVar[tuple[str, ...]] = ("taleo.net", "tbe.taleo.net")
    body_markers: ClassVar[tuple[str, ...]] = (
        "careersection",
        "taleo",
        "oracletaleocwsv2",
    )

    builtin_selectors: ClassVar[tuple[SelectorSet, ...]] = (
        SelectorSet(
            container="tr.oracletaleocwsv2-accordion-head, #jobList tbody tr",
            title="a.oracletaleocwsv2-head-link, td:nth-of-type(1) a, a",
            url="a.oracletaleocwsv2-head-link, td:nth-of-type(1) a, a",
            location="td.oracletaleocwsv2-accordion-head-info span, td:nth-of-type(2)",
            date="td:nth-of-type(3)",
        ),
        SelectorSet(
            container="li.jobResultItem, div.job-tile",
            title="a.jobtitle, h3 a, a",
            url="a.jobtitle, h3 a, a",
            location=".joblocation, .job-location",
            date=".jobdate, .job-date",
        ),
    )

    def wait_for_selector(self) -> str | None:
        return "#jobList, .oracletaleocwsv2-accordion-head, li.jobResultItem"

    @property
    def tier(self) -> ExtractionTier:
        return ExtractionTier.API if self._used_api else ExtractionTier.CSS_SELECTOR

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._used_api = False

    def _rest_endpoint(self) -> str | None:
        """Build the tenant's requisition endpoint, when the URL reveals one."""
        parsed = urlparse(self.company.career_url)
        match = _CAREER_SECTION_RE.search(parsed.path)
        if not match:
            return None
        origin = f"{parsed.scheme}://{parsed.netloc}"
        return urljoin(
            origin,
            f"/careersection/rest/jobboard/searchjobs?"
            f"lang=en&portal={match.group(1)}&limit=200",
        )

    def fetch(self) -> FetchResult:
        endpoint = self._rest_endpoint()
        if endpoint:
            try:
                payload = self.fetcher.fetch_json(endpoint)
                self._used_api = True
                return FetchResult(
                    url=endpoint,
                    final_url=endpoint,
                    status_code=200,
                    text="",
                    content_type="application/json",
                    strategy=ScrapingStrategy.API,
                    fetch_ms=0,
                    json_body=payload,
                )
            except Exception as exc:
                # Very common — most tenants have the endpoint disabled. Debug
                # level, because a warning here would fire on every scan of
                # every Taleo company and train people to ignore warnings.
                self.log.debug("taleo.rest_unavailable", error=str(exc))
                self._used_api = False
        return super().fetch()

    def extract_jobs(self, result: FetchResult) -> list[RawJob]:
        if result.is_json:
            return self._extract_from_api(result)
        return super().extract_jobs(result)

    def _extract_from_api(self, result: FetchResult) -> list[RawJob]:
        payload: Any = result.json_body or {}
        requisitions = payload.get("requisitionList", []) if isinstance(payload, dict) else []
        origin = f"{urlparse(self.company.career_url).scheme}://{urlparse(self.company.career_url).netloc}"

        jobs: list[RawJob] = []
        for entry in requisitions:
            if not isinstance(entry, dict):
                continue
            contest_id = entry.get("contestNo") or entry.get("jobId")
            jobs.append(
                RawJob(
                    title=clean_text(entry.get("title")),
                    url=urljoin(origin, f"/careersection/jobdetail.ftl?job={contest_id}")
                    if contest_id
                    else None,
                    external_id=str(contest_id) if contest_id else None,
                    location=clean_text(entry.get("location")),
                    department=clean_text(entry.get("department")),
                    posted_at=entry.get("postedDate"),
                    raw=entry,
                )
            )
        return jobs
