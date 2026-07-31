"""Extraction scoring.

The single most important idea in the ladder: **a non-empty result is not a
successful result.** An over-broad selector happily returns 40 "jobs" that are
really navigation links, and without scoring, that outcome is indistinguishable
from a good scrape — the selector's confidence stays high, nothing triggers
relearning, and the user quietly stops receiving postings.

The score answers "does this look like a job board?" using signals that are
cheap and do not require knowing the right answer.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from app.core.config import get_settings
from app.scrapers.base import RawJob

#: Text that appears in navigation and chrome, never in a real job title.
_CHROME_PHRASES = (
    "view all",
    "see all",
    "learn more",
    "read more",
    "apply now",
    "sign in",
    "log in",
    "search jobs",
    "all jobs",
    "back to",
    "see full",
    "view details",
    "role description",
    "full description",
    "next page",
    "previous",
    "cookie",
    "privacy policy",
    "terms of",
    "home",
)

#: Words that make a string look like a job title. Absence is not disqualifying
#: — plenty of real titles are just "Analyst" — but presence is good evidence.
_ROLE_WORDS = (
    "engineer", "developer", "manager", "analyst", "designer", "scientist",
    "specialist", "consultant", "director", "lead", "architect", "administrator",
    "coordinator", "associate", "intern", "officer", "executive", "technician",
    "accountant", "recruiter", "nurse", "teacher", "sales", "marketing",
    "product", "research", "support", "operations", "counsel", "assistant",
)

_TITLE_MIN_LENGTH = 3
_TITLE_MAX_LENGTH = 160


@dataclass(slots=True)
class ExtractionScore:
    confidence: float
    jobs_found: int
    #: Field name -> proportion of postings missing it. Surfaced so a partially
    #: broken selector ("titles fine, locations all empty") is diagnosable
    #: without re-running the scrape by hand.
    missing_fields: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    @property
    def is_acceptable(self) -> bool:
        settings = get_settings()
        return (
            self.jobs_found >= settings.extraction_min_jobs_expected
            and self.confidence >= settings.extraction_min_confidence
        )


def score_extraction(jobs: list[RawJob]) -> ExtractionScore:
    """Rate an extraction between 0 and 1."""
    if not jobs:
        return ExtractionScore(
            confidence=0.0, jobs_found=0, reasons=["no postings extracted"]
        )

    total = len(jobs)
    reasons: list[str] = []

    # -- Required fields (60% of the score) --------------------------------
    # Title and URL are what a posting *is*; nothing else can compensate.
    with_title = sum(1 for j in jobs if j.title and j.title.strip())
    with_url = sum(1 for j in jobs if j.url)
    required = 0.35 * (with_title / total) + 0.25 * (with_url / total)

    # -- Optional enrichment (20%) -----------------------------------------
    with_location = sum(1 for j in jobs if j.location)
    with_date = sum(1 for j in jobs if j.posted_at)
    with_description = sum(1 for j in jobs if j.description)
    optional = (
        0.10 * (with_location / total)
        + 0.05 * (with_date / total)
        + 0.05 * (with_description / total)
    )

    # -- Plausibility (20%) ------------------------------------------------
    plausible = sum(1 for j in jobs if _looks_like_job_title(j.title))
    plausibility = 0.20 * (plausible / total)
    confidence = required + optional + plausibility

    # -- Penalties ---------------------------------------------------------
    # A result where most "titles" do not read like job titles is the classic
    # over-broad selector: it scores full marks on required fields (every
    # navigation link has text and an href) and would otherwise clear the
    # threshold on that alone. Losing the plausibility component is not enough
    # of a signal — the whole result has to be discounted.
    if plausible / total < 0.5:
        confidence *= 0.5
        reasons.append("most titles do not read like job titles")
    # Repeated identical titles are the fingerprint of a selector that latched
    # onto a template element rather than the listing.
    title_counts = Counter(_normalize(j.title) for j in jobs)
    most_common = title_counts.most_common(1)[0][1] if title_counts else 0
    if total >= 3 and most_common / total > 0.5:
        confidence *= 0.4
        reasons.append("more than half the titles are identical")

    unique_urls = len({j.url for j in jobs if j.url})
    if with_url and unique_urls == 1 and total > 1:
        confidence *= 0.3
        reasons.append("every posting points at the same URL")

    if with_url / total < 0.5:
        reasons.append("most postings have no link")

    missing = {
        "title": round(1 - with_title / total, 3),
        "url": round(1 - with_url / total, 3),
        "location": round(1 - with_location / total, 3),
        "posted_at": round(1 - with_date / total, 3),
        "description": round(1 - with_description / total, 3),
    }

    return ExtractionScore(
        confidence=round(min(1.0, max(0.0, confidence)), 4),
        jobs_found=total,
        missing_fields={k: v for k, v in missing.items() if v > 0},
        reasons=reasons,
    )


def _normalize(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _looks_like_job_title(title: str | None) -> bool:
    text = _normalize(title)
    if not text or not (_TITLE_MIN_LENGTH <= len(text) <= _TITLE_MAX_LENGTH):
        return False
    if any(phrase in text for phrase in _CHROME_PHRASES):
        return False
    # A title that is one word and not a recognisable role is usually chrome
    # ("Careers", "Locations"); two or more words is weak but real evidence.
    if any(word in text for word in _ROLE_WORDS):
        return True
    return len(text.split()) >= 2
