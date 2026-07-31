"""Normalisation: :class:`RawJob` in, ``Job`` rows out.

The boundary between "what the site said" and "what we believe". Everything
before this point preserves source text verbatim; everything after works with
typed, comparable fields.

Site-agnostic by design — date formats and location strings vary by *site*, not
by ATS, so doing this per adapter would mean nine copies of the same parsers
drifting apart (see ``BaseScraper.normalize``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.logging import get_logger
from app.models.enums import ExtractionTier
from app.models.job import Job
from app.normalization.dates import parse_posted_date, parse_posted_datetime
from app.normalization.fields import detect_skills, parse_employment_type, parse_seniority
from app.normalization.hashing import compute_content_hash
from app.normalization.location import parse_location
from app.normalization.salary import parse_salary
from app.scrapers.base import RawJob
from app.utils.text import clean_text, truncate
from app.utils.urls import canonicalize_url

if TYPE_CHECKING:
    from app.models.company import Company

logger = get_logger(__name__)

#: Column limits from the schema. Truncating here rather than letting the
#: database reject the insert means one absurd posting cannot fail an entire
#: company's scan.
_TITLE_LIMIT = 512
_URL_LIMIT = 2048
_LOCATION_LIMIT = 512


def normalize_jobs(
    raws: list[RawJob],
    *,
    company: Company,
    tier: ExtractionTier = ExtractionTier.CSS_SELECTOR,
) -> list[Job]:
    """Normalise a batch, dropping anything unusable.

    Within-batch duplicates are removed here so that the database's unique
    constraint never has to reject rows from a single insert — a listing that
    shows the same posting under two departments is common and is not an error.
    """
    jobs: list[Job] = []
    seen_hashes: set[str] = set()

    for raw in raws:
        job = normalize_job(raw, company=company, tier=tier)
        if job is None:
            continue
        if job.content_hash in seen_hashes:
            continue
        seen_hashes.add(job.content_hash)
        jobs.append(job)

    if len(jobs) < len(raws):
        logger.debug(
            "normalize.filtered",
            company=company.name,
            received=len(raws),
            kept=len(jobs),
        )
    return jobs


def normalize_job(
    raw: RawJob,
    *,
    company: Company,
    tier: ExtractionTier = ExtractionTier.CSS_SELECTOR,
) -> Job | None:
    """Normalise one posting, or return ``None`` if it is not usable."""
    title = clean_text(raw.title)
    if not title or not raw.url:
        return None

    url = canonicalize_url(raw.url, base=company.career_url)
    if not url:
        return None

    location = parse_location(raw.location, hint=raw.remote)
    salary = parse_salary(raw.salary)
    description = clean_text(raw.description) or None
    requirements = clean_text(raw.requirements) or None

    content_hash = compute_content_hash(
        company_id=company.id,
        title=title,
        location=location.raw,
        url=url,
        external_id=raw.external_id,
    )

    return Job(
        company_id=company.id,
        external_job_id=truncate(raw.external_id, 255) if raw.external_id else None,
        title=truncate(title, _TITLE_LIMIT),
        url=truncate(url, _URL_LIMIT),
        location_raw=truncate(location.raw, _LOCATION_LIMIT) if location.raw else None,
        location_city=location.city,
        location_region=location.region,
        location_country=location.country,
        remote_type=location.remote_type,
        employment_type=parse_employment_type(raw.employment_type, title, description),
        seniority=parse_seniority(title, description),
        description=description,
        requirements=requirements,
        detected_skills=detect_skills(title, description, requirements),
        salary_min=salary.minimum,
        salary_max=salary.maximum,
        salary_currency=salary.currency,
        salary_period=salary.period,
        salary_raw=truncate(salary.raw, 255) if salary.raw else None,
        posted_date=parse_posted_date(raw.posted_at),
        posted_at=parse_posted_datetime(raw.posted_at),
        content_hash=content_hash,
        # The upstream payload is kept verbatim so a future extractor
        # improvement can be backfilled without re-scraping (ARCHITECTURE §9).
        raw_json=_serialisable(raw),
        extraction_tier=tier,
    )


def _serialisable(raw: RawJob) -> dict:
    """Reduce the raw payload to something JSONB will accept.

    Bounded on purpose: some ATS payloads embed the full HTML description twice
    over, and storing that unbounded on every row is a fast route to a table
    that is mostly dead weight.
    """
    payload = dict(raw.raw) if isinstance(raw.raw, dict) else {}
    payload.setdefault("_source_title", raw.title)
    payload.setdefault("_source_url", raw.url)
    payload.setdefault("_source_posted_at", raw.posted_at)

    trimmed: dict = {}
    for key, value in payload.items():
        if isinstance(value, str) and len(value) > 20_000:
            trimmed[key] = value[:20_000]
        elif isinstance(value, (str, int, float, bool, type(None), list, dict)):
            trimmed[key] = value
        else:
            trimmed[key] = str(value)
    return trimmed
