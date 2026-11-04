"""Oracle Fusion Recruiting Cloud ("Candidate Experience").

The site at ``<tenant>.fa.oraclecloud.com/hcmUI/CandidateExperience/...`` is a
JS-rendered SPA whose location filter lives in client-side state, so the page
source carries no postings and the URL's own filters cannot be replayed by
fetching it. The shell calls a documented REST API, which this adapter calls
instead: one request returns the whole filtered board with stable ids.

Filters travel in a single ``finder`` parameter rather than as query
parameters — ``findReqs;siteNumber=CX_1001,selectedLocationsFacet=...`` — which
is why the site's ``locationId`` has to be lifted out of the career URL and
re-expressed rather than passed through.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar
from urllib.parse import parse_qs, quote, urlparse

from app.core.errors import PermanentFetchError
from app.core.logging import get_logger
from app.models.enums import ATSType, ExtractionTier, ScrapingStrategy
from app.scrapers.base import BaseScraper, RawJob
from app.scrapers.fetcher import FetchResult
from app.utils.text import clean_text, strip_html

logger = get_logger(__name__)

#: ``jpmc.fa.oraclecloud.com`` and ``egug.fa.us2.oraclecloud.com`` are both
#: real: the region label between ``fa`` and the domain is optional.
_HOST_RE = re.compile(r"^(?P<tenant>[\w-]+)\.fa(?:\.[\w-]+)?\.oraclecloud\.com$")
#: Many tenants are white-labelled behind the company's own domain, which
#: serves the UI but not the REST API. The tenant's real host appears in that
#: page's HTML, because the page loads its own assets from it.
_TENANT_IN_PAGE_RE = re.compile(
    r"https://[\w-]+\.fa(?:\.[\w-]+)?\.oraclecloud\.com"
)
#: ``/hcmUI/CandidateExperience/en/sites/CX_1001/jobs`` — the site id follows
#: the ``sites`` segment and is what every finder query is scoped to.
_SITE_RE = re.compile(r"/sites/(?P<site>[\w-]+)")

#: The API answers a single request with the whole board, so this is a sanity
#: ceiling rather than a page size: a tenant reporting a wrong count should not
#: have us ask for a million rows.
MAX_RESULTS = 500


class OracleScraper(BaseScraper):
    ats_type: ClassVar[ATSType] = ATSType.ORACLE
    default_strategy: ClassVar[ScrapingStrategy] = ScrapingStrategy.API
    supports_api: ClassVar[bool] = True
    host_markers: ClassVar[tuple[str, ...]] = ("fa.oraclecloud.com", "oraclecloud.com")
    #: Structural, for sites served from a vanity domain in front of Oracle.
    body_markers: ClassVar[tuple[str, ...]] = (
        "CandidateExperience",
        "hcmRestApi",
        "recruitingCEJobRequisitions",
    )

    @property
    def tier(self) -> ExtractionTier:
        return ExtractionTier.API

    # -- Endpoint reconstruction ------------------------------------------

    def _parts(self) -> tuple[str, str]:
        """Return ``(api_origin, site_id)``.

        The API origin is not always the career URL's own: a white-labelled
        tenant serves the Candidate Experience UI from the company's domain
        while the REST API stays on ``*.oraclecloud.com``, and asking the
        vanity host for it earns a redirect to the site's 404 page.
        """
        parsed = urlparse(self.company.career_url)
        match = _SITE_RE.search(parsed.path)
        if not match:
            raise PermanentFetchError(
                "Oracle Fusion URL carries no site id", url=self.company.career_url
            )
        site = match.group("site")

        if _HOST_RE.match(parsed.hostname or ""):
            return f"{parsed.scheme}://{parsed.netloc}", site
        return self._resolve_vanity_origin(), site

    def _resolve_vanity_origin(self) -> str:
        """Find the tenant's real host behind a company-branded domain.

        Cached on the company as ``api_endpoint`` after the first scan, so this
        costs one extra page fetch once rather than one per run.
        """
        if self.company.api_endpoint:
            return self.company.api_endpoint

        result = self.fetcher.fetch(self.company.career_url)
        found = _TENANT_IN_PAGE_RE.search(result.text or "")
        if not found:
            raise PermanentFetchError(
                "no Oracle Fusion tenant found behind this domain",
                url=self.company.career_url,
            )
        origin = found.group(0)
        self.company.api_endpoint = origin
        logger.info("oracle.tenant_resolved", vanity=self.company.career_url, origin=origin)
        return origin

    def _site_origin(self) -> str:
        """Where a *person* should be sent, which is where they came from.

        A white-labelled tenant's own branded job page lives under the company
        domain at ``/<locale>/sites/<site>/job/<id>`` — without the ``/hcmUI/
        CandidateExperience`` prefix the Oracle-hosted sites use. Sending the
        prefixed path to the vanity host redirects to its 404 page, which
        answers 200, so neither shape can be checked by status alone.
        """
        parsed = urlparse(self.company.career_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _job_path(self, job_id: object, site: str) -> str:
        parsed = urlparse(self.company.career_url)
        if _HOST_RE.match(parsed.hostname or ""):
            return f"/hcmUI/CandidateExperience/en/sites/{site}/job/{job_id}"
        # Preserve the locale segment the career URL itself uses.
        locale = next(
            (seg for seg in parsed.path.split("/") if re.fullmatch(r"[a-z]{2}(_[A-Z]{2})?", seg)),
            "en",
        )
        return f"/{locale}/sites/{site}/job/{job_id}"

    def _finder_parts(self) -> list[str]:
        """Scope the query the way the career URL does.

        The site expresses a city filter as ``locationId``; the API calls the
        same thing ``selectedLocationsFacet``. Without the translation we would
        pull the tenant's entire global board — 7,126 postings where the user
        asked for 161 — and lean on local filtering to undo it.
        """
        _origin, site = self._parts()
        parts = [f"siteNumber={site}"]

        query = parse_qs(urlparse(self.company.career_url).query)
        location_id = (query.get("locationId") or [""])[0].strip()
        if location_id:
            parts.append(f"selectedLocationsFacet={location_id}")
        keyword = (query.get("keyword") or [""])[0].strip()
        if keyword:
            parts.append(f"keyword={keyword}")
        return parts

    def _requisitions_url(self, extra: list[str]) -> str:
        origin, _site = self._parts()
        finder = "findReqs;" + ",".join([*self._finder_parts(), *extra])
        return (
            f"{origin}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
            f"?onlyData=true&expand=requisitionList&finder={quote(finder)}"
        )

    @staticmethod
    def _first_item(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        items = payload.get("items") or []
        return items[0] if items and isinstance(items[0], dict) else {}

    # -- Fetch -------------------------------------------------------------

    def fetch(self) -> FetchResult:
        """Ask how many postings match, then ask for exactly that many.

        Two requests rather than one: the API caps an unbounded query at a
        handful of rows, and requesting a fixed large limit would both overfetch
        small boards and silently truncate large ones.
        """
        probe = self._first_item(self.fetcher.fetch_json(self._requisitions_url(["limit=1"])))
        total = int(probe.get("TotalJobsCount") or 0)
        if total == 0:
            logger.info("oracle.no_matches", url=self.company.career_url)
            return self._result([], 0)

        wanted = min(total, MAX_RESULTS)
        if total > MAX_RESULTS:
            logger.warning("oracle.results_capped", total=total, cap=MAX_RESULTS)

        full = self._first_item(
            self.fetcher.fetch_json(self._requisitions_url([f"limit={wanted}"]))
        )
        postings = [p for p in (full.get("requisitionList") or []) if isinstance(p, dict)]
        return self._result(postings, total)

    def _result(self, postings: list[dict[str, Any]], total: int) -> FetchResult:
        url = self._requisitions_url(["limit=1"])
        return FetchResult(
            url=url,
            final_url=url,
            status_code=200,
            text="",
            content_type="application/json",
            strategy=ScrapingStrategy.API,
            fetch_ms=0,
            json_body={"requisitionList": postings, "total": total},
        )

    # -- Extract -----------------------------------------------------------

    def extract_jobs(self, result: FetchResult) -> list[RawJob]:
        payload: Any = result.json_body or {}
        entries = payload.get("requisitionList", []) if isinstance(payload, dict) else []
        _api_origin, site = self._parts()

        jobs: list[RawJob] = []
        for entry in entries:
            job_id = entry.get("Id")
            jobs.append(
                RawJob(
                    title=clean_text(entry.get("Title")),
                    # The SPA's own deep link. Built rather than read: the list
                    # response carries no URL field at all.
                    url=(
                        f"{self._site_origin()}{self._job_path(job_id, site)}"
                        if job_id
                        else None
                    ),
                    external_id=str(job_id) if job_id else None,
                    location=clean_text(entry.get("PrimaryLocation")),
                    department=clean_text(entry.get("JobFamily") or entry.get("JobFunction")),
                    employment_type=clean_text(entry.get("JobSchedule")),
                    # An absolute date, unlike most boards' "Posted 3 Days Ago".
                    posted_at=clean_text(entry.get("PostedDate")),
                    remote=clean_text(entry.get("WorkplaceType")),
                    description=strip_html(entry.get("ShortDescriptionStr")),
                    raw=entry,
                )
            )
        return jobs
