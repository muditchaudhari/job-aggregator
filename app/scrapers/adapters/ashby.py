"""Ashby.

Ashby's job-board API is public and returns the whole board in one call.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from app.core.errors import PermanentFetchError
from app.models.enums import ATSType, ExtractionTier, ScrapingStrategy
from app.scrapers.base import BaseScraper, RawJob
from app.scrapers.fetcher import FetchResult
from app.utils.text import clean_text, strip_html

_TOKEN_PATTERNS = (
    re.compile(r"jobs\.ashbyhq\.com/([\w.-]+)"),
    re.compile(r"api\.ashbyhq\.com/posting-api/job-board/([\w.-]+)"),
)

API_TEMPLATE = (
    "https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
)


def extract_board_token(url: str, body: str = "") -> str | None:
    for pattern in _TOKEN_PATTERNS:
        for candidate in (url, body):
            if not candidate:
                continue
            match = pattern.search(candidate)
            if match:
                return match.group(1)
    return None


class AshbyScraper(BaseScraper):
    ats_type: ClassVar[ATSType] = ATSType.ASHBY
    default_strategy: ClassVar[ScrapingStrategy] = ScrapingStrategy.API
    supports_api: ClassVar[bool] = True
    host_markers: ClassVar[tuple[str, ...]] = ("ashbyhq.com",)
    body_markers: ClassVar[tuple[str, ...]] = ("ashbyhq.com", "_ashby_embed")

    @property
    def tier(self) -> ExtractionTier:
        return ExtractionTier.API

    def _token(self) -> str:
        token = self.company.board_token or extract_board_token(self.company.career_url)
        if not token:
            raise PermanentFetchError(
                "no Ashby board token available", url=self.company.career_url
            )
        return token

    def fetch(self) -> FetchResult:
        endpoint = self.company.api_endpoint or API_TEMPLATE.format(token=self._token())
        payload = self.fetcher.fetch_json(endpoint)
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

    def extract_jobs(self, result: FetchResult) -> list[RawJob]:
        payload: Any = result.json_body or {}
        entries = payload.get("jobs", []) if isinstance(payload, dict) else []

        jobs: list[RawJob] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            # Ashby marks unlisted postings in-band rather than omitting them.
            if entry.get("isListed") is False:
                continue
            jobs.append(
                RawJob(
                    title=clean_text(entry.get("title")),
                    url=entry.get("jobUrl") or entry.get("applyUrl"),
                    external_id=entry.get("id"),
                    location=clean_text(entry.get("location")),
                    description=strip_html(
                        entry.get("descriptionPlain") or entry.get("descriptionHtml")
                    ),
                    department=clean_text(entry.get("department") or entry.get("team")),
                    employment_type=clean_text(entry.get("employmentType")),
                    remote="remote" if entry.get("isRemote") else None,
                    salary=self._compensation(entry),
                    posted_at=entry.get("publishedAt") or entry.get("updatedAt"),
                    raw=entry,
                )
            )
        return jobs

    @staticmethod
    def _compensation(entry: dict[str, Any]) -> str | None:
        summary = (entry.get("compensation") or {}).get("compensationTierSummary")
        return clean_text(summary) or None
