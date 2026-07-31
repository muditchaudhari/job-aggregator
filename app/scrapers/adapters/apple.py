"""Apple.

Two different sources, because Apple splits the data:

* the **listing** at ``jobs.apple.com/*/search`` is server-rendered cards, so
  the shared HTML path (plus ``?page=`` pagination) reads it fine;
* the **detail** page is client-rendered — 194 KB of markup containing about
  2 KB of text — so scraping it would need a browser. Its backing API
  (``/api/v1/jobDetails/{id}``) returns the whole posting as JSON instead.

That detail payload is what makes experience filtering possible here: the
listing card carries only a title, team, date and location, while
``minimumQualifications`` says "12+ years of software engineering experience".
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from app.extractors.selector_extractor import SelectorSet
from app.models.enums import ATSType, ScrapingStrategy
from app.scrapers.adapters.html_base import HtmlListingScraper
from app.scrapers.base import JobDetail
from app.utils.text import clean_text, strip_html

#: ``/en-in/details/200673779-0321/senior-software-engineer`` -> the id.
_DETAIL_ID_RE = re.compile(r"/details/([\w-]+)")

DETAIL_API = "https://jobs.apple.com/api/v1/jobDetails/{job_id}"


class AppleScraper(HtmlListingScraper):
    ats_type: ClassVar[ATSType] = ATSType.APPLE
    default_strategy: ClassVar[ScrapingStrategy] = ScrapingStrategy.HTTP
    host_markers: ClassVar[tuple[str, ...]] = ("jobs.apple.com",)
    body_markers: ClassVar[tuple[str, ...]] = ("jobs.apple.com", "apple-jobs")

    builtin_selectors: ClassVar[tuple[SelectorSet, ...]] = (
        SelectorSet(
            container="div[class*='job-list-item']",
            title="h3 a, a[class*='link-inline']",
            url="h3 a, a[class*='link-inline']",
            location="[class*='location-sub'], [class*='job-title-location']",
            date="[class*='job-posted-date']",
            department="[class*='team-name']",
        ),
    )

    @staticmethod
    def detail_id(url: str) -> str | None:
        match = _DETAIL_ID_RE.search(url)
        return match.group(1) if match else None

    def fetch_detail(self, url: str) -> JobDetail | None:
        """Pull the full posting from the JSON API behind the detail page."""
        job_id = self.detail_id(url)
        if not job_id:
            return None

        try:
            payload = self.fetcher.fetch_json(DETAIL_API.format(job_id=job_id))
        except Exception as exc:
            self.log.debug("apple.detail_unavailable", job_id=job_id, error=str(exc))
            return None

        record = payload.get("res") if isinstance(payload, dict) else None
        if not isinstance(record, dict):
            return None

        summary = strip_html(record.get("jobSummary"))
        body = strip_html(record.get("description"))
        minimum = strip_html(record.get("minimumQualifications"))
        preferred = strip_html(record.get("preferredQualifications"))

        return JobDetail(
            description="\n\n".join(part for part in (summary, body) if part) or None,
            # Qualifications are where the years requirement lives, and keeping
            # them separate means the experience parser reads the requirement
            # rather than the marketing paragraph above it.
            requirements="\n\n".join(part for part in (minimum, preferred) if part)
            or None,
            employment_type=clean_text(record.get("employmentType")) or None,
            posted_at=record.get("postDateInGMT") or record.get("postingDate"),
            raw=_trim(record),
        )


def _trim(record: dict[str, Any]) -> dict[str, Any]:
    """Drop the bulky localisation blocks; keep what a human would want."""
    keep = (
        "id", "jobNumber", "positionId", "postingTitle", "employmentType",
        "jobType", "homeOffice", "postDateInGMT",
    )
    return {key: record[key] for key in keep if key in record}
