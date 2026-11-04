"""SAP SuccessFactors.

Two generations live behind one platform name.

The older sites are server-rendered, which is the good news; the bad news is
that the markup is heavily themed per customer and comes in at least three
generations, so several selector candidates are carried rather than one.

Newer tenants run **Career Site Builder**, where the search page ships no
postings at all and fetches them from ``/services/recruiting/v1/jobs``. The
selector path extracts nothing there — it is not a themed variant but a
different site. So the JSON endpoint is tried first and the HTML path remains
the fallback.
"""

from __future__ import annotations

import json
import re
from html import unescape
from typing import Any, ClassVar
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

from app.core.logging import get_logger
from app.extractors.selector_extractor import SelectorSet
from app.models.enums import ATSType, ScrapingStrategy
from app.scrapers.adapters.html_base import HtmlListingScraper
from app.scrapers.base import RawJob
from app.scrapers.fetcher import FetchResult
from app.utils.text import clean_text

PAGE_SIZE = 20
MAX_PAGES = 15

logger = get_logger(__name__)

#: Career Site Builder's listing endpoint, relative to the site's own origin.
CSB_PATH = "/services/recruiting/v1/jobs"
#: Fixed by the server, not by us: ``size``, ``limit`` and every other spelling
#: of it are ignored and ten rows come back regardless.
CSB_PAGE_SIZE = 10
CSB_MAX_PAGES = 40
#: Tried in order when a tenant's locale cannot be read from its URL. A guess
#: about spelling, not about any particular customer: the endpoint requires
#: ``locale`` and answers 200 with zero rows for a wrong one, so there is no
#: way to ask it which it wants. Whichever returns rows is cached per company.
CSB_LOCALE_FALLBACKS = ("en_GB", "en_US")


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

    # -- Career Site Builder (JSON) ---------------------------------------

    def _csb_origin(self) -> str:
        parsed = urlparse(self.company.career_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _csb_filters(self) -> dict[str, Any]:
        """Carry the search page's own facet filters into the request.

        The career URL holds them as a JSON blob in ``facetFilters``, e.g.
        ``{"jobLocationCity":["Bangalore"]}``. Dropping them would fetch the
        tenant's whole global board — 1,039 postings against 100 in Bangalore.
        """
        raw = (parse_qs(urlparse(self.company.career_url).query).get("facetFilters") or [""])[0]
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError:
            logger.warning("successfactors.bad_facet_filters", raw=raw[:120])
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _csb_page(self, page: int, locale: str) -> Any:
        return self.fetcher.post_json(
            self._csb_origin() + CSB_PATH,
            {
                "facetFilters": self._csb_filters(),
                "locale": locale,
                # NOT "from"/"offset"/"start": those are accepted and ignored,
                # so every page came back as page one. Measured by distinct ids
                # across ten pages — 73 with this key, 16 with the others,
                # against 16 for the same call repeated.
                "pageNumber": page,
            },
        )

    def _csb_locale_candidates(self) -> list[str]:
        """Locales to try, best guess first.

        ``locale`` is required, and a wrong one is not an error: the endpoint
        answers 200 with ``totalJobs: 0``. So it cannot be hardcoded — a
        tenant on ``en_US`` would look like an empty board rather than a
        misconfiguration — and it cannot be probed by status code either.

        A locale the career URL states is trusted; otherwise the common
        spellings are tried in turn and the one that returns rows is cached.
        """
        cached = (self.company.config or {}).get("csb_locale")
        if cached:
            return [str(cached)]

        found: list[str] = []
        parsed = urlparse(self.company.career_url)
        for chunk in (*parsed.path.split("/"), *parse_qs(parsed.query).get("locale", [])):
            if re.fullmatch(r"[a-z]{2}_[A-Z]{2}", chunk or ""):
                found.append(chunk)
        for fallback in CSB_LOCALE_FALLBACKS:
            if fallback not in found:
                found.append(fallback)
        return found

    def _remember_locale(self, locale: str) -> None:
        # Reassigned rather than mutated so SQLAlchemy sees the JSON change.
        self.company.config = {**(self.company.config or {}), "csb_locale": locale}

    def _fetch_csb(self) -> FetchResult | None:
        """Return the JSON board, or ``None`` on a tenant without this endpoint."""
        first = None
        locale = ""
        for candidate in self._csb_locale_candidates():
            try:
                payload = self._csb_page(0, candidate)
            except Exception as exc:
                self.log.debug("successfactors.no_csb_endpoint", error=str(exc))
                return None
            if not isinstance(payload, dict) or "jobSearchResult" not in payload:
                return None
            if payload.get("jobSearchResult"):
                first, locale = payload, candidate
                break
            self.log.debug("successfactors.csb_empty_for_locale", locale=candidate)

        if first is None:
            # The endpoint exists and answers, but no locale we know returns
            # rows. Fall through to the HTML path rather than report an empty
            # board, which would read as "this company has no openings".
            return None
        self._remember_locale(locale)

        total = int(first.get("totalJobs") or 0)
        rows: list[dict[str, Any]] = list(first.get("jobSearchResult") or [])
        pages = min(CSB_MAX_PAGES, -(-total // CSB_PAGE_SIZE) if total else 1)
        for page in range(1, pages):
            try:
                payload = self._csb_page(page, locale)
            except Exception as exc:
                self.log.debug("successfactors.csb_page_failed", page=page, error=str(exc))
                break
            batch = payload.get("jobSearchResult") or [] if isinstance(payload, dict) else []
            if not batch:
                break
            rows.extend(batch)

        url = self._csb_origin() + CSB_PATH
        return FetchResult(
            url=url, final_url=url, status_code=200, text="",
            content_type="application/json", strategy=ScrapingStrategy.API,
            fetch_ms=0, json_body={"jobSearchResult": rows, "totalJobs": total},
        )

    def _jobs_from_csb(self, payload: dict[str, Any]) -> list[RawJob]:
        jobs: list[RawJob] = []
        origin = self._csb_origin()
        default_locale = (
            (self.company.config or {}).get("csb_locale") or CSB_LOCALE_FALLBACKS[0]
        )
        for row in payload.get("jobSearchResult") or []:
            entry = row.get("response") if isinstance(row, dict) else None
            if not isinstance(entry, dict):
                continue
            job_id = entry.get("id")
            # Each posting names its own locale, and the link carries it as a
            # suffix — so this is read per posting rather than assumed.
            locales = entry.get("supportedLocales") or []
            locale = str(locales[0]) if locales else default_locale
            # Already percent-encoded by the API, but carrying HTML entities
            # ("&amp;") that would otherwise land in the link verbatim.
            slug = unescape(str(entry.get("urlTitle") or entry.get("unifiedUrlTitle") or ""))
            jobs.append(
                RawJob(
                    title=clean_text(unescape(str(entry.get("unifiedStandardTitle") or ""))),
                    url=f"{origin}/job/{slug}/{job_id}-{locale}" if job_id and slug else None,
                    external_id=str(job_id) if job_id else None,
                    location=clean_text(", ".join(entry.get("jobLocationShort") or [])),
                    # "mfield1" is a tenant-configured custom field, not part
                    # of the schema: it holds the job category on the tenants
                    # seen so far, but another could put anything there, so a
                    # long value is treated as not-a-category and dropped.
                    department=self._csb_category(entry),
                    posted_at=clean_text(entry.get("unifiedStandardStart")),
                    raw=entry,
                )
            )
        return jobs

    @staticmethod
    def _csb_category(entry: dict[str, Any]) -> str | None:
        values = [v for v in (entry.get("mfield1") or []) if isinstance(v, str)]
        joined = clean_text(", ".join(values))
        return joined if joined and len(joined) <= 80 else None

    def fetch(self) -> FetchResult:
        """Fetch page one, then follow the offset pagination.

        Subsequent pages are concatenated into the first result's HTML rather
        than being modelled as separate fetches. It is a blunt approach, but the
        selectors are container-scoped, so concatenated documents extract
        exactly the same as separate ones — and it keeps the ``fetch → extract``
        contract to a single ``FetchResult``.
        """
        csb = self._fetch_csb()
        if csb is not None:
            return csb

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
        if isinstance(result.json_body, dict) and "jobSearchResult" in result.json_body:
            jobs = self._jobs_from_csb(result.json_body)
        else:
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
