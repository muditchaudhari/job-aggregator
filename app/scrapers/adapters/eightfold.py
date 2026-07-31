"""Eightfold AI.

The careers platform behind Microsoft and a long tail of large employers.
Pages are fully client-rendered — the served HTML carries no postings at all —
so the obvious approach is a headless render. It is also unnecessary: the page
fetches its results from ``/api/pcsx/search``, which answers plain HTTP
requests with no cookie, token or session.

That is a ~10-second render replaced by a ~0.3-second request, and the JSON
carries fields the DOM never shows (epoch timestamps, an ATS job id, a remote
flag).

Two things are worth knowing if this ever breaks:

* ``/api/apply/v2/jobs`` — the endpoint Eightfold is better known for — returns
  ``403 Not authorized for PCSX`` on these tenants. The error is pointing at
  the endpoint above, not refusing you.
* ``num`` is accepted and ignored; the page size is fixed at 10.
"""

from __future__ import annotations

from typing import Any, ClassVar
from urllib.parse import parse_qsl, urljoin, urlparse

from app.core.logging import get_logger
from app.models.enums import ATSType, ExtractionTier, ScrapingStrategy
from app.scrapers.base import BaseScraper, JobDetail, RawJob
from app.scrapers.fetcher import FetchResult
from app.utils.text import clean_text, strip_html

logger = get_logger(__name__)

PAGE_SIZE = 10
MAX_PAGES = 30

SEARCH_PATH = "/api/pcsx/search"
DETAIL_PATH = "/api/pcsx/position_details"

#: Query parameters that are part of the *search*, as opposed to UI state.
#: Passed straight through so the filters in the URL you pasted — location,
#: radius, remote — are the filters the API applies.
_SEARCH_PARAMS = frozenset(
    {
        "query", "location", "sort_by", "hl", "domain", "seniority",
        "filter_distance", "filter_include_remote", "filter_include_relocation",
        "department", "workLocationOption", "triggerGoButton",
    }
)


class EightfoldScraper(BaseScraper):
    ats_type: ClassVar[ATSType] = ATSType.EIGHTFOLD
    default_strategy: ClassVar[ScrapingStrategy] = ScrapingStrategy.API
    supports_api: ClassVar[bool] = True
    host_markers: ClassVar[tuple[str, ...]] = ("eightfold.ai",)
    body_markers: ClassVar[tuple[str, ...]] = (
        "eightfold",
        "/api/pcsx",
        "pcsx",
    )

    @property
    def tier(self) -> ExtractionTier:
        return ExtractionTier.API

    # -- Request construction ---------------------------------------------

    def _origin(self) -> str:
        parsed = urlparse(self.company.career_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _base_params(self) -> dict[str, str]:
        parsed = urlparse(self.company.career_url)
        params = {
            key: value
            for key, value in parse_qsl(parsed.query)
            if key in _SEARCH_PARAMS and value
        }
        # ``domain`` is what tells Eightfold which tenant's board to search.
        # It is usually in the URL; when it is not, the host is a good guess.
        params.setdefault("domain", _domain_from_host(parsed.hostname or ""))
        params.setdefault("query", "")
        return params

    def fetch(self) -> FetchResult:
        endpoint = self._origin() + SEARCH_PATH
        base = self._base_params()

        collected: list[dict[str, Any]] = []
        total: int | None = None

        for page in range(MAX_PAGES):
            payload = self.fetcher.fetch_json(
                endpoint, params={**base, "start": str(page * PAGE_SIZE)}
            )
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                break

            positions = [p for p in (data.get("positions") or []) if isinstance(p, dict)]
            if not positions:
                break
            collected.extend(positions)

            if total is None:
                total = int(data.get("count") or 0)
            if len(collected) >= (total or 0):
                break
        else:
            logger.warning("eightfold.pagination_capped", collected=len(collected))

        self.log.debug("eightfold.search", jobs=len(collected), total=total)
        return FetchResult(
            url=endpoint,
            final_url=endpoint,
            status_code=200,
            text="",
            content_type="application/json",
            strategy=ScrapingStrategy.API,
            fetch_ms=0,
            json_body={"positions": collected, "total": total or len(collected)},
        )

    # -- Extraction --------------------------------------------------------

    def extract_jobs(self, result: FetchResult) -> list[RawJob]:
        payload = result.json_body
        if not isinstance(payload, dict):
            return []

        origin = self._origin()
        jobs: list[RawJob] = []
        for entry in payload.get("positions", []):
            title = clean_text(entry.get("name"))
            if not title:
                continue
            url = entry.get("positionUrl") or ""
            jobs.append(
                RawJob(
                    title=title,
                    url=urljoin(origin, url) if url else None,
                    external_id=str(
                        entry.get("displayJobId") or entry.get("atsJobId") or entry.get("id") or ""
                    )
                    or None,
                    location=_join_locations(entry.get("locations")),
                    department=clean_text(entry.get("department")),
                    remote=_remote_hint(entry),
                    # Epoch seconds; normalisation turns it into a timestamp.
                    posted_at=str(entry.get("postedTs") or entry.get("creationTs") or "")
                    or None,
                    raw=entry,
                )
            )
        return jobs

    # -- Detail ------------------------------------------------------------

    def fetch_detail(self, url: str) -> JobDetail | None:
        """Full posting text, for the experience filter."""
        position_id = url.rstrip("/").rsplit("/", 1)[-1]
        if not position_id.isdigit():
            return None

        params = {"position_id": position_id, "domain": self._base_params()["domain"]}
        try:
            payload = self.fetcher.fetch_json(self._origin() + DETAIL_PATH, params=params)
        except Exception as exc:
            self.log.debug("eightfold.detail_unavailable", id=position_id, error=str(exc))
            return None

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return None

        body = strip_html(data.get("job_description") or data.get("jobDescription"))
        if not body:
            return None
        return JobDetail(
            description=body,
            employment_type=clean_text(data.get("employmentType")) or None,
            raw={"source": "pcsx/position_details"},
        )


def _domain_from_host(hostname: str) -> str:
    """``apply.careers.microsoft.com`` -> ``microsoft.com``."""
    parts = [p for p in hostname.split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else hostname


def _join_locations(value: Any) -> str | None:
    if isinstance(value, list):
        places = [clean_text(v) for v in value if isinstance(v, str) and v.strip()]
        # Multi-location postings list every site; keeping a few keeps the
        # string matchable without turning it into a paragraph.
        return "; ".join(places[:3]) or None
    return clean_text(value) if isinstance(value, str) else None


def _remote_hint(entry: dict[str, Any]) -> str | None:
    for key in ("workLocationOption", "locationFlexibility"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None
