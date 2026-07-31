"""Uber.

``jobs.uber.com`` serves a 205 KB shell whose every anchor is navigation or
social media — the listings arrive by XHR afterwards. Rendering it works;
calling the endpoint it renders *from* is far better:

    GET /api/jobs/search/?location=Bengaluru

No cookie, no token, and the payload is richer than the page. It carries the
full job description inline (~7 kB), which means the experience filter works
here without any per-job detail fetches at all.

Note the trailing slash on the path. Uber answers ``/api/jobs/search/`` and
redirects ``/api/jobs/search``; more importantly the *board* URL behaves the
same way — ``/en/jobs/`` is a 200 and ``/en/jobs`` is a 403 — which is why
``canonicalize_url`` preserves trailing slashes.
"""

from __future__ import annotations

from typing import Any, ClassVar
from urllib.parse import parse_qsl, urljoin, urlparse

from app.core.logging import get_logger
from app.models.enums import ATSType, ExtractionTier, ScrapingStrategy
from app.scrapers.base import BaseScraper, RawJob
from app.scrapers.fetcher import FetchResult
from app.utils.text import clean_text, strip_html

logger = get_logger(__name__)

SEARCH_PATH = "/api/jobs/search/"
MAX_PAGES = 30

#: Query parameters the board URL and the API share.
_SEARCH_PARAMS = ("location", "query", "team", "department", "radius")


class UberScraper(BaseScraper):
    ats_type: ClassVar[ATSType] = ATSType.UBER
    default_strategy: ClassVar[ScrapingStrategy] = ScrapingStrategy.API
    supports_api: ClassVar[bool] = True
    host_markers: ClassVar[tuple[str, ...]] = ("jobs.uber.com",)

    @property
    def tier(self) -> ExtractionTier:
        return ExtractionTier.API

    def _origin(self) -> str:
        parsed = urlparse(self.company.career_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _params(self) -> dict[str, str]:
        query = dict(parse_qsl(urlparse(self.company.career_url).query))
        return {key: query[key] for key in _SEARCH_PARAMS if query.get(key)}

    def fetch(self) -> FetchResult:
        endpoint = self._origin() + SEARCH_PATH
        base = self._params()

        collected: list[dict[str, Any]] = []
        total_pages: int | None = None

        for page in range(MAX_PAGES):
            payload = self.fetcher.fetch_json(
                endpoint, params={**base, "page": str(page)}
            )
            if not isinstance(payload, dict):
                break

            jobs = [j for j in (payload.get("jobs") or []) if isinstance(j, dict)]
            if not jobs:
                break
            collected.extend(jobs)

            if total_pages is None:
                total_pages = int(payload.get("totalPages") or 1)
            if page + 1 >= total_pages:
                break
        else:
            logger.warning("uber.pagination_capped", collected=len(collected))

        self.log.debug("uber.search", jobs=len(collected), pages=total_pages)
        return FetchResult(
            url=endpoint,
            final_url=endpoint,
            status_code=200,
            text="",
            content_type="application/json",
            strategy=ScrapingStrategy.API,
            fetch_ms=0,
            json_body={"jobs": collected},
        )

    def extract_jobs(self, result: FetchResult) -> list[RawJob]:
        payload = result.json_body
        if not isinstance(payload, dict):
            return []

        origin = self._origin()
        jobs: list[RawJob] = []
        for entry in payload.get("jobs", []):
            title = clean_text(entry.get("Title"))
            if not title:
                continue
            jobs.append(
                RawJob(
                    title=title,
                    url=_job_url(entry, origin),
                    external_id=str(entry.get("Id") or entry.get("Reference") or "") or None,
                    location=_join_locations(entry.get("Locations")),
                    # The listing carries the full spec, so the experience
                    # filter needs no follow-up request for this board.
                    description=strip_html(entry.get("Description")),
                    requirements=strip_html(entry.get("AdditionalDescription1")),
                    department=_first(entry.get("Teams")),
                    employment_type=clean_text(entry.get("ContractType")),
                    remote="remote" if entry.get("Remote") else None,
                    salary=_salary(entry.get("Salary")),
                    posted_at=entry.get("DisplayDate"),
                    raw={
                        key: entry.get(key)
                        for key in ("Id", "Reference", "ExperienceLevel", "WorkPattern")
                    },
                )
            )
        return jobs


def _job_url(entry: dict[str, Any], origin: str) -> str | None:
    urls = entry.get("Urls")
    if isinstance(urls, list):
        default = next(
            (u for u in urls if isinstance(u, dict) and u.get("IsDefault")),
            urls[0] if urls and isinstance(urls[0], dict) else None,
        )
        if isinstance(default, dict) and default.get("Url"):
            return urljoin(origin, str(default["Url"]))
    job_id = entry.get("Id")
    return urljoin(origin, f"/en/jobs/{job_id}/") if job_id else None


def _join_locations(value: Any) -> str | None:
    if not isinstance(value, list):
        return None
    places = []
    for item in value[:3]:
        if not isinstance(item, dict):
            continue
        text = item.get("Address") or ", ".join(
            part
            for part in (item.get("City"), item.get("Region"), item.get("Country"))
            if part
        )
        if text:
            places.append(clean_text(text))
    return "; ".join(places) or None


def _first(value: Any) -> str | None:
    if isinstance(value, list) and value:
        return clean_text(str(value[0])) or None
    return None


def _salary(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    low, high = value.get("MinValue"), value.get("MaxValue")
    if low is None and high is None:
        return clean_text(value.get("Description")) or None
    currency = value.get("Currency") or ""
    return f"{currency} {low or ''}-{high or ''}".strip()
