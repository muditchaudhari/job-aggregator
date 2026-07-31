"""Greenhouse.

Greenhouse publishes an unauthenticated board API, so this adapter never parses
HTML. That is worth the special case: the API returns stable requisition ids and
ISO timestamps, which are the two things HTML scraping can never recover
reliably and which deduplication depends on.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from app.core.errors import PermanentFetchError
from app.models.enums import ATSType, ExtractionTier, ScrapingStrategy
from app.scrapers.base import BaseScraper, RawJob
from app.scrapers.fetcher import FetchResult
from app.utils.text import clean_text, strip_html

#: Order matters. The embed forms must be tried before the bare board URL,
#: because ``boards.greenhouse.io/embed/job_board/js?for=acme`` also matches
#: the bare pattern — and captures the literal segment ``embed`` as the token.
#: The negative lookahead on the last pattern is a second line of defence.
_BOARD_TOKEN_PATTERNS = (
    re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?(?:[^\"'&]*&)?for=([\w.-]+)"),
    re.compile(r"api\.greenhouse\.io/v1/boards/([\w.-]+)"),
    # Any board host: boards., job-boards., and the EU variants
    # (job-boards.eu.greenhouse.io). All of them are served by the same
    # boards-api.greenhouse.io — there is no separate EU API host, so only the
    # token extraction needed to know about the subdomain.
    re.compile(r"(?:job-)?boards(?:\.[a-z]{2})?\.greenhouse\.io/(?!embed(?:/|\?|$))([\w.-]+)"),
)

API_TEMPLATE = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"


def extract_board_token(url: str, body: str = "") -> str | None:
    """Find the board slug in a URL, or failing that, in the page source.

    The body fallback matters for companies that embed Greenhouse in their own
    careers page: the visible URL is ``acme.com/careers`` and the only place the
    token appears is the embed script tag.
    """
    for pattern in _BOARD_TOKEN_PATTERNS:
        for candidate in (url, body):
            if not candidate:
                continue
            match = pattern.search(candidate)
            if match:
                return match.group(1)
    return None


class GreenhouseScraper(BaseScraper):
    ats_type: ClassVar[ATSType] = ATSType.GREENHOUSE
    default_strategy: ClassVar[ScrapingStrategy] = ScrapingStrategy.API
    supports_api: ClassVar[bool] = True
    host_markers: ClassVar[tuple[str, ...]] = ("greenhouse.io",)
    body_markers: ClassVar[tuple[str, ...]] = (
        "greenhouse.io/embed/job_board",
        "boards-api.greenhouse.io",
        "grnhse_app",
    )

    @property
    def tier(self) -> ExtractionTier:
        return ExtractionTier.API

    def _token(self) -> str:
        token = self.company.board_token or extract_board_token(self.company.career_url)
        if not token:
            raise PermanentFetchError(
                "no Greenhouse board token available", url=self.company.career_url
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
            metadata = {
                item.get("name"): item.get("value")
                for item in entry.get("metadata") or []
                if isinstance(item, dict)
            }
            jobs.append(
                RawJob(
                    title=clean_text(entry.get("title")),
                    url=entry.get("absolute_url"),
                    external_id=str(entry.get("id")) if entry.get("id") else None,
                    location=clean_text((entry.get("location") or {}).get("name")),
                    # ``content`` is HTML-escaped HTML — unescaped by clean_text
                    # inside strip_html, which is why the double pass is needed.
                    description=strip_html(entry.get("content")),
                    department=self._first_department(entry),
                    posted_at=entry.get("updated_at") or entry.get("first_published"),
                    employment_type=metadata.get("Employment Type"),
                    salary=metadata.get("Salary Range"),
                    raw=entry,
                )
            )
        return jobs

    @staticmethod
    def _first_department(entry: dict[str, Any]) -> str | None:
        departments = entry.get("departments") or []
        if departments and isinstance(departments[0], dict):
            return clean_text(departments[0].get("name")) or None
        return None
