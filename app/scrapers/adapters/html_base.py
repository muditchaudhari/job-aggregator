"""Shared behaviour for adapters that read HTML rather than an API.

These platforms have no public listing API, but they *do* ship recognisable
markup, so each adapter carries a small set of hand-written selector candidates.
Trying those first means a known ATS costs zero LLM calls even the very first
time we see a particular tenant — the built-ins are effectively a pre-seeded
version of what the learner would otherwise have to discover.

When every built-in comes up empty, the adapter hands off to the full extraction
ladder, which brings embedded JSON, learned selectors, and finally the LLM into
play.
"""

from __future__ import annotations

from typing import ClassVar
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.core.logging import get_logger
from app.extractors.selector_extractor import SelectorSet, extract_with_selectors
from app.models.enums import ExtractionTier
from app.scrapers.base import BaseScraper, RawJob
from app.scrapers.fetcher import FetchResult

logger = get_logger(__name__)

#: Upper bound on pages followed. A guard against a site that ignores the page
#: parameter and serves the same content forever, not a target.
MAX_HTML_PAGES = 15


class HtmlListingScraper(BaseScraper):
    """Base for HTML-scraped ATS platforms."""

    #: Ordered candidates, most specific first. The first set that yields any
    #: usable posting wins; later entries exist because these platforms ship
    #: several markup generations simultaneously across tenants.
    builtin_selectors: ClassVar[tuple[SelectorSet, ...]] = ()

    #: Whether to follow ``?page=2,3,...``. Most listings paginate this way;
    #: adapters that handle their own paging (SuccessFactors) turn it off.
    paginates: ClassVar[bool] = True

    @property
    def tier(self) -> ExtractionTier:
        return ExtractionTier.CSS_SELECTOR

    @staticmethod
    def _with_page(url: str, page: int) -> str:
        parsed = urlparse(url)
        params = dict(parse_qsl(parsed.query))
        params["page"] = str(page)
        return urlunparse(parsed._replace(query=urlencode(params)))

    def fetch(self) -> FetchResult:
        """Fetch page one, then follow ``?page=`` while new postings appear.

        Stops as soon as a page contributes no URL we have not already seen,
        which is exactly what happens when a site ignores the parameter and
        re-serves page one — so this is safe to attempt everywhere rather than
        needing per-site configuration.

        Pages are concatenated into a single document. Selectors are
        container-scoped and results are collapsed by URL, so a concatenated
        page extracts identically to separate ones.
        """
        first = super().fetch()
        if not self.paginates:
            return first

        seen = {job.url for job in self.extract_jobs(first) if job.url}
        if not seen:
            return first

        combined = [first.text]
        for page in range(2, MAX_HTML_PAGES + 1):
            try:
                nxt = self.fetcher.fetch(
                    self._with_page(self.company.career_url, page),
                    strategy=first.strategy,
                )
            except Exception as exc:
                self.log.debug("html.pagination_stopped", page=page, error=str(exc))
                break

            fresh = {job.url for job in self.extract_jobs(nxt) if job.url} - seen
            if not fresh:
                self.log.debug("html.pagination_exhausted", page=page)
                break
            seen |= fresh
            combined.append(nxt.text)

        if len(combined) > 1:
            self.log.debug("html.paginated", pages=len(combined), jobs=len(seen))
            first.text = "\n".join(combined)
        return first

    def extract_jobs(self, result: FetchResult) -> list[RawJob]:
        base_url = result.final_url or result.url

        for index, candidate in enumerate(self.builtin_selectors):
            jobs = [
                job
                for job in extract_with_selectors(result.text, base_url, candidate)
                if job.is_usable()
            ]
            if jobs:
                self.log.debug(
                    "builtin_selector.hit", index=index, container=candidate.container
                )
                return jobs

        self.log.debug("builtin_selector.miss", candidates=len(self.builtin_selectors))
        return []
