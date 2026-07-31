"""Workday.

Workday career sites are Angular shells — the HTML contains no postings at all.
Rendering them works but is slow and fragile. Every tenant, however, serves the
same internal ``/wday/cxs/`` JSON endpoint that the shell itself calls, so this
adapter reconstructs that endpoint from the public URL and talks to it directly.

That is the difference between a 20-second render returning 20 visible rows and
a 300 ms request returning the whole board with stable ids.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar
from urllib.parse import urljoin, urlparse

from app.core.errors import PermanentFetchError
from app.core.logging import get_logger
from app.models.enums import ATSType, ExtractionTier, ScrapingStrategy
from app.scrapers.base import BaseScraper, RawJob
from app.scrapers.fetcher import FetchResult
from app.utils.text import clean_text

logger = get_logger(__name__)

#: ``https://acme.wd1.myworkdayjobs.com/en-US/External`` — tenant is the
#: leftmost host label, the site id is the last meaningful path segment. The
#: optional locale segment in between is skipped.
_HOST_RE = re.compile(r"^(?P<tenant>[\w-]+)\.(?P<dc>wd\d+)\.myworkdayjobs\.com$")
_LOCALE_RE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2})?$")

PAGE_SIZE = 20
MAX_PAGES = 50


class WorkdayScraper(BaseScraper):
    ats_type: ClassVar[ATSType] = ATSType.WORKDAY
    default_strategy: ClassVar[ScrapingStrategy] = ScrapingStrategy.API
    supports_api: ClassVar[bool] = True
    host_markers: ClassVar[tuple[str, ...]] = ("myworkdayjobs.com", "myworkdaysite.com")
    #: Structural markers only. The bare word "workday" matched any page that
    #: merely *linked* to a Workday apply URL — which is most Phenom sites —
    #: and sent them to an adapter that cannot parse their host.
    body_markers: ClassVar[tuple[str, ...]] = (
        "myworkdayjobs.com",
        "wday/cxs",
        "wd-airbrake",
    )

    @property
    def tier(self) -> ExtractionTier:
        return ExtractionTier.API

    # -- Endpoint reconstruction ------------------------------------------

    def _parts(self) -> tuple[str, str, str]:
        """Return ``(origin, tenant, site_id)`` derived from the career URL."""
        parsed = urlparse(self.company.career_url)
        match = _HOST_RE.match(parsed.hostname or "")
        if not match:
            raise PermanentFetchError(
                "not a recognisable Workday host", url=self.company.career_url
            )

        segments = [seg for seg in parsed.path.split("/") if seg]
        # Drop a locale prefix if present; the site id is what remains first.
        if segments and _LOCALE_RE.match(segments[0]):
            segments = segments[1:]
        if not segments:
            raise PermanentFetchError(
                "Workday URL carries no site id", url=self.company.career_url
            )

        origin = f"{parsed.scheme}://{parsed.netloc}"
        return origin, match.group("tenant"), segments[0]

    def _endpoint(self) -> str:
        if self.company.api_endpoint:
            return self.company.api_endpoint
        origin, tenant, site = self._parts()
        return f"{origin}/wday/cxs/{tenant}/{site}/jobs"

    # -- Fetch -------------------------------------------------------------

    def fetch(self) -> FetchResult:
        """Page through the CXS endpoint until the board is exhausted.

        ``MAX_PAGES`` is a hard stop, not a nicety: a tenant that ignores the
        offset (or reports a wrong ``total``) would otherwise loop forever
        against someone else's server.
        """
        endpoint = self._endpoint()
        collected: list[dict[str, Any]] = []
        total: int | None = None

        for page in range(MAX_PAGES):
            offset = page * PAGE_SIZE
            payload = self.fetcher.post_json(
                endpoint,
                {
                    "appliedFacets": {},
                    "limit": PAGE_SIZE,
                    "offset": offset,
                    "searchText": "",
                },
            )
            if not isinstance(payload, dict):
                break

            postings = payload.get("jobPostings") or []
            collected.extend(p for p in postings if isinstance(p, dict))

            if total is None:
                total = int(payload.get("total") or 0)
            if len(postings) < PAGE_SIZE or len(collected) >= (total or 0):
                break
        else:
            logger.warning(
                "workday.pagination_capped", endpoint=endpoint, collected=len(collected)
            )

        return FetchResult(
            url=endpoint,
            final_url=endpoint,
            status_code=200,
            text="",
            content_type="application/json",
            strategy=ScrapingStrategy.API,
            fetch_ms=0,
            json_body={"jobPostings": collected, "total": total or len(collected)},
        )

    # -- Extract -----------------------------------------------------------

    def extract_jobs(self, result: FetchResult) -> list[RawJob]:
        payload: Any = result.json_body or {}
        entries = payload.get("jobPostings", []) if isinstance(payload, dict) else []
        base = self.company.career_url

        jobs: list[RawJob] = []
        for entry in entries:
            external_path = entry.get("externalPath") or ""
            jobs.append(
                RawJob(
                    title=clean_text(entry.get("title")),
                    url=urljoin(base, external_path) if external_path else None,
                    # ``bulletFields`` is where Workday puts the requisition
                    # number; there is no dedicated id field on the list view.
                    external_id=self._requisition_id(entry),
                    location=clean_text(entry.get("locationsText")),
                    # e.g. "Posted 3 Days Ago" — relative, handled downstream.
                    posted_at=clean_text(entry.get("postedOn")),
                    employment_type=clean_text(entry.get("timeType")),
                    raw=entry,
                )
            )
        return jobs

    @staticmethod
    def _requisition_id(entry: dict[str, Any]) -> str | None:
        bullets = entry.get("bulletFields") or []
        if bullets and isinstance(bullets[0], str):
            return bullets[0]
        path = entry.get("externalPath") or ""
        match = re.search(r"_(R-?\d+)", path)
        return match.group(1) if match else None
