"""Lever.

Like Greenhouse, Lever exposes a public postings API keyed by a company slug.
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
    re.compile(r"jobs\.(?:eu\.)?lever\.co/([\w.-]+)"),
    re.compile(r"api\.lever\.co/v0/postings/([\w.-]+)"),
)

API_TEMPLATE = "https://api.lever.co/v0/postings/{token}?mode=json"


def extract_board_token(url: str, body: str = "") -> str | None:
    for pattern in _TOKEN_PATTERNS:
        for candidate in (url, body):
            if not candidate:
                continue
            match = pattern.search(candidate)
            if match:
                return match.group(1)
    return None


class LeverScraper(BaseScraper):
    ats_type: ClassVar[ATSType] = ATSType.LEVER
    default_strategy: ClassVar[ScrapingStrategy] = ScrapingStrategy.API
    supports_api: ClassVar[bool] = True
    host_markers: ClassVar[tuple[str, ...]] = ("lever.co", "jobs.lever.co")
    body_markers: ClassVar[tuple[str, ...]] = ("api.lever.co", "lever-jobs", "data-qa=\"posting\"")

    @property
    def tier(self) -> ExtractionTier:
        return ExtractionTier.API

    def _token(self) -> str:
        token = self.company.board_token or extract_board_token(self.company.career_url)
        if not token:
            raise PermanentFetchError(
                "no Lever board token available", url=self.company.career_url
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
        payload: Any = result.json_body or []
        if not isinstance(payload, list):
            return []

        jobs: list[RawJob] = []
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            categories = entry.get("categories") or {}
            jobs.append(
                RawJob(
                    title=clean_text(entry.get("text")),
                    url=entry.get("hostedUrl") or entry.get("applyUrl"),
                    external_id=entry.get("id"),
                    location=clean_text(categories.get("location")),
                    description=strip_html(
                        entry.get("descriptionPlain") or entry.get("description")
                    ),
                    requirements=self._requirements(entry),
                    department=clean_text(
                        categories.get("team") or categories.get("department")
                    ),
                    employment_type=clean_text(categories.get("commitment")),
                    remote=clean_text(entry.get("workplaceType")),
                    salary=self._salary(entry),
                    # Lever reports epoch milliseconds; normalisation handles
                    # the conversion, so it is passed through as a string.
                    posted_at=str(entry["createdAt"]) if entry.get("createdAt") else None,
                    raw=entry,
                )
            )
        return jobs

    @staticmethod
    def _requirements(entry: dict[str, Any]) -> str | None:
        """Lever splits a posting into labelled sections; pull the useful one."""
        for section in entry.get("lists") or []:
            if not isinstance(section, dict):
                continue
            label = (section.get("text") or "").lower()
            if any(word in label for word in ("requirement", "qualification", "looking for")):
                return strip_html(section.get("content"))
        return None

    @staticmethod
    def _salary(entry: dict[str, Any]) -> str | None:
        salary = entry.get("salaryRange")
        if not isinstance(salary, dict):
            return None
        minimum, maximum = salary.get("min"), salary.get("max")
        currency = salary.get("currency", "")
        if minimum is None and maximum is None:
            return None
        return f"{currency} {minimum or ''}-{maximum or ''}".strip()
