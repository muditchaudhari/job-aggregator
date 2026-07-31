"""Phenom People.

The careers front-end behind a large share of Fortune-500 sites (Adobe among
them). It renders client-side, but every page ships its current result set
inline as ``phApp.ddo`` — a JSON blob with clean, well-named fields — so this
adapter never needs a browser.

Worth its own adapter rather than leaning on the generic embedded-JSON tier for
two reasons: the blob is nested under a key the generic harvester would have to
guess at, and pagination is a simple ``?from=`` offset that only makes sense if
you know the shape.

A Phenom site often fronts a different ATS underneath — Adobe's apply links go
to Workday. That is invisible here and does not matter: the listing is Phenom's.
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.core.logging import get_logger
from app.models.enums import ATSType, ExtractionTier, ScrapingStrategy
from app.scrapers.base import BaseScraper, RawJob
from app.scrapers.fetcher import FetchResult
from app.utils.text import clean_text, strip_html

logger = get_logger(__name__)

#: ``phApp.ddo = { ... };`` — captured with a balanced-brace scan rather than a
#: lazy regex, because the blob routinely contains ``};`` inside nested strings.
_DDO_START = re.compile(r"phApp\.ddo\s*=\s*\{")

PAGE_SIZE = 10
#: Phenom's server-rendered page size is fixed at 10 — `size`/`pageSize` are
#: ignored — so a large board genuinely needs this many requests. The cap is a
#: runaway guard, not a target; the loop stops as soon as `totalHits` is met.
MAX_PAGES = 100

#: The JSON widget endpoint accepts a real page size, so a large board costs a
#: handful of requests rather than dozens.
WIDGET_PAGE_SIZE = 100
MAX_WIDGET_PAGES = 30

#: Query parameters that mean "the user asked for a subset of this board".
#:
#: Phenom applies these only to the first server-rendered page: request
#: ``?keywords=x&from=10`` and the keyword is silently dropped, so page two
#: onwards returns the *whole* board. Paginating a filtered URL therefore turns
#: a 10-result search into a few hundred unrelated postings — worse than
#: useless, because it looks like it worked. When a filter is present we take
#: page one and stop.
_FILTER_PARAMS = frozenset(
    {"keywords", "q", "query", "search", "location", "category", "department", "facets"}
)


def has_search_filter(url: str) -> bool:
    query = {key.lower() for key, value in parse_qsl(urlparse(url).query) if value}
    return bool(query & _FILTER_PARAMS)


def extract_ddo(html: str) -> dict[str, Any] | None:
    """Pull ``phApp.ddo`` out of a page.

    Brace-counting rather than regex: the payload embeds job descriptions that
    contain both braces and ``};``, so any non-greedy pattern truncates it
    mid-object and the parse fails on perfectly good pages.
    """
    match = _DDO_START.search(html)
    if not match:
        return None

    start = match.end() - 1
    depth, in_string, escaped, quote = 0, False, False, ""

    for index in range(start, len(html)):
        char = html[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                in_string = False
            continue
        if char in "\"'":
            in_string, quote = True, char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start : index + 1])
                except (json.JSONDecodeError, ValueError) as exc:
                    logger.debug("phenom.ddo_parse_failed", error=str(exc))
                    return None
    return None


def _jobs_from_ddo(ddo: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """Return ``(jobs, total)``. The blob nests results a couple of ways."""
    for key in ("eagerLoadRefineSearch", "refineSearch"):
        section = ddo.get(key)
        if not isinstance(section, dict):
            continue
        total = int(section.get("totalHits") or section.get("hits") or 0)
        data = section.get("data")
        jobs = data.get("jobs") if isinstance(data, dict) else section.get("jobs")
        if isinstance(jobs, list):
            return [job for job in jobs if isinstance(job, dict)], total
    return [], 0


class PhenomScraper(BaseScraper):
    ats_type: ClassVar[ATSType] = ATSType.PHENOM
    default_strategy: ClassVar[ScrapingStrategy] = ScrapingStrategy.HTTP
    host_markers: ClassVar[tuple[str, ...]] = ("phenompeople.com",)
    body_markers: ClassVar[tuple[str, ...]] = (
        "phapp.ddo",
        "phenompeople",
        "ph-search-results",
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._used_widget = False

    @property
    def tier(self) -> ExtractionTier:
        # Honest about which route was taken: the widget endpoint is a real
        # JSON API, while the fallback reads structured data embedded in the
        # page. Both are trustworthy; they are not the same rung.
        return ExtractionTier.API if self._used_widget else ExtractionTier.EMBEDDED_JSON

    @staticmethod
    def _page_url(url: str, offset: int) -> str:
        parsed = urlparse(url)
        params = dict(parse_qsl(parsed.query))
        params["from"] = str(offset)
        params.setdefault("s", "1")
        return urlunparse(parsed._replace(query=urlencode(params)))

    # -- Widget API (preferred) -------------------------------------------

    def _widget_endpoint(self) -> str:
        parsed = urlparse(self.company.career_url)
        return f"{parsed.scheme}://{parsed.netloc}/widgets"

    def _widget_body(self, keywords: str, offset: int) -> dict[str, Any]:
        return {
            "lang": "en_us",
            "deviceType": "desktop",
            "country": "us",
            "pageName": "search-results",
            "ddoKey": "refineSearch",
            "sortBy": "",
            "subsearch": "",
            "from": offset,
            "jobs": True,
            "counts": True,
            "all_fields": ["category", "country", "state", "city", "type"],
            "size": WIDGET_PAGE_SIZE,
            "clearAll": False,
            "jdsource": "facets",
            "isSliderEnable": False,
            "pageId": "page18",
            "siteType": "external",
            "keywords": keywords,
            "global": True,
        }

    def _fetch_via_widget(self, keywords: str) -> list[dict[str, Any]] | None:
        """Page the JSON widget endpoint. Returns ``None`` if unavailable.

        Strongly preferred over scraping the rendered pages: it accepts a page
        size of 100 rather than a fixed 10 (787 jobs in 8 requests instead of
        79), and — unlike the rendered pagination — it keeps the keyword filter
        applied on every page.

        Returning ``None`` rather than raising is deliberate. The request body
        carries fields that may not be universal across Phenom tenants, so a
        rejection means "fall back to the rendered pages", not "this board is
        broken".
        """
        endpoint = self._widget_endpoint()
        collected: list[dict[str, Any]] = []
        total: int | None = None

        for page in range(MAX_WIDGET_PAGES):
            offset = page * WIDGET_PAGE_SIZE
            try:
                payload = self.fetcher.post_json(endpoint, self._widget_body(keywords, offset))
            except Exception as exc:
                if page == 0:
                    self.log.debug("phenom.widget_unavailable", error=str(exc))
                    return None
                self.log.debug("phenom.widget_stopped", offset=offset, error=str(exc))
                break

            section = payload.get("refineSearch") if isinstance(payload, dict) else None
            if not isinstance(section, dict):
                return None if page == 0 else collected

            jobs = (section.get("data") or {}).get("jobs") or []
            jobs = [job for job in jobs if isinstance(job, dict)]
            if not jobs:
                break
            collected.extend(jobs)

            if total is None:
                total = int(section.get("totalHits") or 0)
            if len(collected) >= (total or 0):
                break

        self.log.debug("phenom.widget", jobs=len(collected), total=total)
        return collected or None

    # -- Rendered pages (fallback) ----------------------------------------

    def fetch(self) -> FetchResult:
        """Widget API first; fall back to walking the rendered ``from=`` pages."""
        keywords = dict(parse_qsl(urlparse(self.company.career_url).query)).get(
            "keywords", ""
        )
        via_widget = self._fetch_via_widget(keywords)
        if via_widget is not None:
            self._used_widget = True
            return FetchResult(
                url=self._widget_endpoint(),
                final_url=self._widget_endpoint(),
                status_code=200,
                text="",
                content_type="application/json",
                strategy=ScrapingStrategy.API,
                fetch_ms=0,
                json_body={"jobs": via_widget, "total": len(via_widget)},
            )

        first = self.fetcher.fetch(self.company.career_url, strategy=ScrapingStrategy.HTTP)
        ddo = extract_ddo(first.text)
        if ddo is None:
            # No blob: let the ladder fall through to the HTML tiers rather
            # than reporting an empty board as a success.
            return first

        collected, total = _jobs_from_ddo(ddo)
        self.log.debug("phenom.page", offset=0, got=len(collected), total=total)

        if has_search_filter(self.company.career_url):
            self.log.info(
                "phenom.filtered_url_single_page",
                got=len(collected),
                reason="pagination drops the filter; returning the filtered page only",
            )
            first.json_body = {"jobs": collected, "total": len(collected)}
            return first

        for page in range(1, MAX_PAGES):
            if len(collected) >= total or not collected:
                break
            offset = page * PAGE_SIZE
            try:
                nxt = self.fetcher.fetch(
                    self._page_url(self.company.career_url, offset),
                    strategy=ScrapingStrategy.HTTP,
                )
            except Exception as exc:
                self.log.debug("phenom.pagination_stopped", offset=offset, error=str(exc))
                break
            page_ddo = extract_ddo(nxt.text)
            if page_ddo is None:
                break
            jobs, _ = _jobs_from_ddo(page_ddo)
            if not jobs:
                break
            collected.extend(jobs)

        first.json_body = {"jobs": collected, "total": total}
        return first

    def extract_jobs(self, result: FetchResult) -> list[RawJob]:
        payload = result.json_body
        if not isinstance(payload, dict):
            # fetch() found no ddo; nothing for this adapter to do.
            return []

        jobs: list[RawJob] = []
        for entry in payload.get("jobs", []):
            title = clean_text(entry.get("title"))
            if not title:
                continue
            jobs.append(
                RawJob(
                    title=title,
                    # The apply link is the only URL guaranteed present, and it
                    # is where a candidate actually needs to end up.
                    url=entry.get("applyUrl") or entry.get("jobDetailUrl"),
                    external_id=str(entry.get("jobId") or entry.get("jobSeqNo") or "") or None,
                    location=clean_text(
                        entry.get("location")
                        or entry.get("cityStateCountry")
                        or entry.get("cityState")
                    ),
                    description=strip_html(entry.get("descriptionTeaser")),
                    department=clean_text(entry.get("category")),
                    employment_type=clean_text(entry.get("type")),
                    posted_at=entry.get("postedDate") or entry.get("dateCreated"),
                    raw=entry,
                )
            )
        return jobs
