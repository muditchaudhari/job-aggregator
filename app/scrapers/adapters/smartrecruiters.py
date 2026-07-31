"""SmartRecruiters.

Public postings API, offset-paginated at 100 records per page.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from app.core.errors import PermanentFetchError
from app.core.logging import get_logger
from app.models.enums import ATSType, ExtractionTier, ScrapingStrategy
from app.scrapers.base import BaseScraper, RawJob
from app.scrapers.fetcher import FetchResult
from app.utils.text import clean_text

logger = get_logger(__name__)

_TOKEN_PATTERNS = (
    re.compile(r"jobs\.smartrecruiters\.com/([\w.-]+)"),
    re.compile(r"careers\.smartrecruiters\.com/([\w.-]+)"),
    re.compile(r"api\.smartrecruiters\.com/v1/companies/([\w.-]+)"),
)

API_TEMPLATE = "https://api.smartrecruiters.com/v1/companies/{token}/postings"
PAGE_SIZE = 100
MAX_PAGES = 20


def extract_board_token(url: str, body: str = "") -> str | None:
    for pattern in _TOKEN_PATTERNS:
        for candidate in (url, body):
            if not candidate:
                continue
            match = pattern.search(candidate)
            if match:
                return match.group(1)
    return None


class SmartRecruitersScraper(BaseScraper):
    ats_type: ClassVar[ATSType] = ATSType.SMARTRECRUITERS
    default_strategy: ClassVar[ScrapingStrategy] = ScrapingStrategy.API
    supports_api: ClassVar[bool] = True
    host_markers: ClassVar[tuple[str, ...]] = ("smartrecruiters.com",)
    body_markers: ClassVar[tuple[str, ...]] = ("smartrecruiters", "sr-jobs")

    @property
    def tier(self) -> ExtractionTier:
        return ExtractionTier.API

    def _token(self) -> str:
        token = self.company.board_token or extract_board_token(self.company.career_url)
        if not token:
            raise PermanentFetchError(
                "no SmartRecruiters company token", url=self.company.career_url
            )
        return token

    def fetch(self) -> FetchResult:
        token = self._token()
        base = self.company.api_endpoint or API_TEMPLATE.format(token=token)

        collected: list[dict[str, Any]] = []
        total: int | None = None
        for page in range(MAX_PAGES):
            offset = page * PAGE_SIZE
            payload = self.fetcher.fetch_json(
                f"{base}?limit={PAGE_SIZE}&offset={offset}"
            )
            if not isinstance(payload, dict):
                break
            content = payload.get("content") or []
            collected.extend(item for item in content if isinstance(item, dict))
            if total is None:
                total = int(payload.get("totalFound") or 0)
            if len(content) < PAGE_SIZE or len(collected) >= (total or 0):
                break
        else:
            logger.warning("smartrecruiters.pagination_capped", token=token)

        return FetchResult(
            url=base,
            final_url=base,
            status_code=200,
            text="",
            content_type="application/json",
            strategy=ScrapingStrategy.API,
            fetch_ms=0,
            json_body={"content": collected, "company": token},
        )

    def extract_jobs(self, result: FetchResult) -> list[RawJob]:
        payload: Any = result.json_body or {}
        entries = payload.get("content", []) if isinstance(payload, dict) else []
        token = payload.get("company") if isinstance(payload, dict) else None

        jobs: list[RawJob] = []
        for entry in entries:
            jobs.append(
                RawJob(
                    title=clean_text(entry.get("name")),
                    url=self._posting_url(entry, token),
                    external_id=str(entry.get("id") or entry.get("uuid") or "") or None,
                    location=self._location(entry),
                    department=clean_text(
                        (entry.get("department") or {}).get("label")
                        if isinstance(entry.get("department"), dict)
                        else None
                    ),
                    employment_type=clean_text(
                        (entry.get("typeOfEmployment") or {}).get("label")
                        if isinstance(entry.get("typeOfEmployment"), dict)
                        else None
                    ),
                    remote="remote"
                    if (entry.get("location") or {}).get("remote")
                    else None,
                    posted_at=entry.get("releasedDate") or entry.get("createdOn"),
                    raw=entry,
                )
            )
        return jobs

    @staticmethod
    def _posting_url(entry: dict[str, Any], token: str | None) -> str | None:
        """Prefer the API's own link; fall back to the canonical board URL."""
        ref = entry.get("ref")
        if isinstance(ref, str) and ref.startswith("http"):
            return ref
        posting_url = entry.get("postingUrl")
        if isinstance(posting_url, str) and posting_url.startswith("http"):
            return posting_url
        if token and entry.get("id"):
            return f"https://jobs.smartrecruiters.com/{token}/{entry['id']}"
        return None

    @staticmethod
    def _location(entry: dict[str, Any]) -> str | None:
        location = entry.get("location") or {}
        if not isinstance(location, dict):
            return None
        parts = [
            location.get("city"),
            location.get("region"),
            location.get("country"),
        ]
        return clean_text(", ".join(p for p in parts if p)) or None
