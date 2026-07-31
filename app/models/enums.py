"""Enumerations shared by models, schemas, and scrapers.

Stored as strings rather than native PostgreSQL enums: adding a new ATS should
be a code change plus a data migration for existing rows, not an
``ALTER TYPE`` that locks the table and cannot be run inside a transaction on
older server versions.
"""

from __future__ import annotations

from enum import StrEnum


class ATSType(StrEnum):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    WORKDAY = "workday"
    SMARTRECRUITERS = "smartrecruiters"
    SUCCESSFACTORS = "successfactors"
    TALEO = "taleo"
    PHENOM = "phenom"
    APPLE = "apple"
    EIGHTFOLD = "eightfold"
    UBER = "uber"
    CUSTOM_REACT = "custom_react"
    GENERIC_HTML = "generic_html"
    UNKNOWN = "unknown"


class ScrapingStrategy(StrEnum):
    #: Hit the platform's JSON API directly. Cheapest and most accurate.
    API = "api"
    #: Plain HTTP GET; the server returns complete markup.
    HTTP = "http"
    #: Headless render required — the listing is built client side.
    PLAYWRIGHT = "playwright"
    #: Not yet determined; the detector will decide on first scan.
    AUTO = "auto"


class ExtractionTier(StrEnum):
    """Rungs of the extraction ladder, cheapest first (see AD-3)."""

    API = "api"
    EMBEDDED_JSON = "embedded_json"
    CSS_SELECTOR = "css_selector"
    XPATH = "xpath"
    LLM = "llm"


class SelectorStrategy(StrEnum):
    CSS = "css"
    XPATH = "xpath"
    JSON_PATH = "json_path"


class SelectorOrigin(StrEnum):
    LLM = "llm"
    MANUAL = "manual"
    BUILTIN = "builtin"


class RemoteType(StrEnum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class RemotePreference(StrEnum):
    REMOTE_ONLY = "remote_only"
    HYBRID_OK = "hybrid_ok"
    ONSITE_OK = "onsite_ok"
    ANY = "any"


class EmploymentType(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    INTERNSHIP = "internship"
    TEMPORARY = "temporary"
    UNKNOWN = "unknown"


class SeniorityLevel(StrEnum):
    INTERN = "intern"
    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    STAFF = "staff"
    PRINCIPAL = "principal"
    LEAD = "lead"
    MANAGER = "manager"
    DIRECTOR = "director"
    UNKNOWN = "unknown"

    @property
    def rank(self) -> int:
        """Ordinal used for "at least this senior" comparisons.

        ``UNKNOWN`` sorts as mid-level so that unlabelled postings are neither
        automatically excluded nor treated as executive roles.
        """
        return _SENIORITY_RANK[self]


_SENIORITY_RANK: dict[SeniorityLevel, int] = {
    SeniorityLevel.INTERN: 0,
    SeniorityLevel.JUNIOR: 1,
    SeniorityLevel.MID: 2,
    SeniorityLevel.UNKNOWN: 2,
    SeniorityLevel.SENIOR: 3,
    SeniorityLevel.LEAD: 4,
    SeniorityLevel.STAFF: 4,
    SeniorityLevel.MANAGER: 5,
    SeniorityLevel.PRINCIPAL: 5,
    SeniorityLevel.DIRECTOR: 6,
}


class ScrapeFrequency(StrEnum):
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    CUSTOM = "custom"

    @property
    def minutes(self) -> int | None:
        return _FREQUENCY_MINUTES[self]


_FREQUENCY_MINUTES: dict[ScrapeFrequency, int | None] = {
    ScrapeFrequency.HOURLY: 60,
    ScrapeFrequency.DAILY: 1440,
    ScrapeFrequency.WEEKLY: 10_080,
    ScrapeFrequency.CUSTOM: None,
}


class ScrapeStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


class NotificationChannel(StrEnum):
    EMAIL = "email"
    RESEND = "resend"
    SLACK = "slack"
    TELEGRAM = "telegram"


class NotificationStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SUPPRESSED = "suppressed"


class SalaryPeriod(StrEnum):
    YEAR = "year"
    MONTH = "month"
    WEEK = "week"
    DAY = "day"
    HOUR = "hour"
    UNKNOWN = "unknown"
