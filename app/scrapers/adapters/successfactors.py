"""SAP SuccessFactors.

SuccessFactors career sites are server-rendered, which is the good news. The bad
news is that the markup is heavily themed per customer and comes in at least
three generations, so several selector candidates are carried rather than one.
Search results are also paginated in the query string.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.extractors.selector_extractor import SelectorSet
from app.models.enums import ATSType, ScrapingStrategy
from app.scrapers.adapters.html_base import HtmlListingScraper
from app.scrapers.base import RawJob
from app.scrapers.fetcher import FetchResult

PAGE_SIZE = 20
MAX_PAGES = 15


class SuccessFactorsScraper(HtmlListingScraper):
    ats_type: ClassVar[ATSType] = ATSType.SUCCESSFACTORS
    default_strategy: ClassVar[ScrapingStrategy] = ScrapingStrategy.HTTP
    host_markers: ClassVar[tuple[str, ...]] = (
        "successfactors.com",
        "successfactors.eu",
        "sapsf.com",
        "sapsf.eu",
        "jobs.sap.com",
    )
    #: "careersection" is deliberately absent: it is a *Taleo* URL segment, and
    #: claiming it here meant whichever adapter came first in the registry won,
    #: sending Taleo tenants to a parser that cannot read them.
    body_markers: ClassVar[tuple[str, ...]] = (
        "successfactors",
        "sapsf",
        "jobsearchresultsform",
    )

    paginates: ClassVar[bool] = False  # handled by startrow offsets below

    builtin_selectors: ClassVar[tuple[SelectorSet, ...]] = (
        # Current "job tile" theme.
        SelectorSet(
            container="li.jobTitle, div.jobTitle",
            title="a.jobTitle-link, a",
            url="a.jobTitle-link, a",
            location="span.jobLocation, .jobFacility",
            date="span.jobDate, .jobDate",
        ),
        # Table-based theme still in use on older tenants.
        SelectorSet(
            container="tr.data-row",
            title="a.jobTitle-link, td.colTitle a, a",
            url="a.jobTitle-link, td.colTitle a, a",
            location="span.jobLocation, td.colLocation",
            date="span.jobDate, td.colDate",
        ),
        # Generic SuccessFactors search result item.
        SelectorSet(
            container="div.searchResultItem, li.job-tile",
            title="a[href*='/job/'], h3 a, a",
            url="a[href*='/job/'], h3 a, a",
            location=".jobLocation, .job-location",
            date=".jobDate, .job-date",
        ),
    )

    def wait_for_selector(self) -> str | None:
        return "a[href*='/job/']"

    def fetch(self) -> FetchResult:
        """Fetch page one, then follow the offset pagination.

        Subsequent pages are concatenated into the first result's HTML rather
        than being modelled as separate fetches. It is a blunt approach, but the
        selectors are container-scoped, so concatenated documents extract
        exactly the same as separate ones — and it keeps the ``fetch → extract``
        contract to a single ``FetchResult``.
        """
        first = super().fetch()
        if not self._is_paginated(first.text):
            return first

        combined = [first.text]
        for page in range(1, MAX_PAGES):
            url = self._page_url(self.company.career_url, page * PAGE_SIZE)
            try:
                nxt = self.fetcher.fetch(url, strategy=ScrapingStrategy.HTTP)
            except Exception as exc:
                self.log.debug("successfactors.pagination_stopped", page=page, error=str(exc))
                break
            if not self._is_paginated(nxt.text) and page > 1:
                combined.append(nxt.text)
                break
            combined.append(nxt.text)

        first.text = "\n".join(combined)
        return first

    @staticmethod
    def _is_paginated(html: str) -> bool:
        return bool(re.search(r"startrow=|paginationLink|next\s*&gt;", html, re.IGNORECASE))

    @staticmethod
    def _page_url(url: str, start_row: int) -> str:
        parsed = urlparse(url)
        params: dict[str, Any] = dict(parse_qsl(parsed.query))
        params["startrow"] = str(start_row)
        return urlunparse(parsed._replace(query=urlencode(params)))

    def extract_jobs(self, result: FetchResult) -> list[RawJob]:
        jobs = super().extract_jobs(result)
        # Concatenated pages can repeat the same posting when a tenant's
        # pagination wraps; dedupe on URL before the shared hash ever sees them.
        seen: set[str] = set()
        unique: list[RawJob] = []
        for job in jobs:
            key = job.url or job.title
            if key in seen:
                continue
            seen.add(key)
            unique.append(job)
        return unique
