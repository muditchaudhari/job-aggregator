"""Embedded JSON extraction (ladder tier 2).

A large share of modern career pages ship their own data inside the document:
schema.org ``JobPosting`` blocks for SEO, Next.js ``__NEXT_DATA__``, Nuxt's
``__NUXT__``, or a bare ``window.__INITIAL_STATE__``. When present this is
strictly better than reading the DOM — the data is already structured, the
field names are stable, and no selector can rot.

Worth trying on every HTML page before selectors, because it costs one pass
over a string we have already downloaded.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from bs4 import BeautifulSoup

from app.core.logging import get_logger
from app.scrapers.base import RawJob
from app.utils.text import clean_text, strip_html
from app.utils.urls import absolutize

if TYPE_CHECKING:
    from app.scrapers.base import JobDetail

logger = get_logger(__name__)

#: Assignments that commonly hold a page's bootstrap state.
_STATE_PATTERNS = (
    re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.DOTALL),
    re.compile(r"window\.__NUXT__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.DOTALL),
    re.compile(r"window\.__PRELOADED_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.DOTALL),
    re.compile(r"window\.__APP_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.DOTALL),
)

#: Keys whose value is plausibly a list of postings.
_LIST_KEYS = (
    "jobs", "jobPostings", "postings", "positions", "openings", "vacancies",
    "jobPosts", "results", "items", "requisitions", "jobList", "searchResults",
)

_TITLE_KEYS = ("title", "name", "jobTitle", "text", "positionTitle", "displayName")
_URL_KEYS = ("url", "absolute_url", "hostedUrl", "applyUrl", "jobUrl", "link",
             "externalPath", "canonicalUrl", "detailUrl", "applyLink")
_LOCATION_KEYS = ("location", "locationName", "locationsText", "city", "office",
                  "primaryLocation", "jobLocation")
_ID_KEYS = ("id", "jobId", "requisitionId", "externalId", "uuid", "refNumber",
            "contestNo", "reqId")
_DATE_KEYS = ("postedAt", "postedOn", "postedDate", "datePosted", "publishedAt",
              "createdAt", "updated_at", "releasedDate", "firstPublished")
_DESCRIPTION_KEYS = ("description", "descriptionPlain", "jobDescription", "content",
                     "descriptionHtml", "summary")


def extract_embedded_jobs(html: str, base_url: str) -> list[RawJob]:
    """Pull postings out of any JSON the page carries.

    Sources are tried most-structured first: JSON-LD is a published schema and
    therefore trustworthy, framework state blobs need shape-guessing.
    """
    jobs = _from_json_ld(html, base_url)
    if jobs:
        logger.debug("embedded_json.hit", source="json-ld", count=len(jobs))
        return jobs

    jobs = _from_next_data(html, base_url)
    if jobs:
        logger.debug("embedded_json.hit", source="__NEXT_DATA__", count=len(jobs))
        return jobs

    jobs = _from_state_blobs(html, base_url)
    if jobs:
        logger.debug("embedded_json.hit", source="state", count=len(jobs))
    return jobs


# --- Sources --------------------------------------------------------------


def _from_json_ld(html: str, base_url: str) -> list[RawJob]:
    soup = BeautifulSoup(html, "lxml")
    jobs: list[RawJob] = []

    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        payload = _safe_json(tag.string or tag.get_text() or "")
        if payload is None:
            continue
        for node in _walk_json_ld(payload):
            job = _job_from_schema_org(node, base_url)
            if job is not None:
                jobs.append(job)
    return jobs


def _walk_json_ld(payload: Any) -> list[dict[str, Any]]:
    """Flatten the several shapes JSON-LD is legally allowed to take."""
    found: list[dict[str, Any]] = []
    if isinstance(payload, list):
        for item in payload:
            found.extend(_walk_json_ld(item))
    elif isinstance(payload, dict):
        node_type = payload.get("@type")
        types = node_type if isinstance(node_type, list) else [node_type]
        if "JobPosting" in types:
            found.append(payload)
        # @graph is the common wrapper when a page declares several entities.
        for key in ("@graph", "itemListElement", "mainEntity"):
            if key in payload:
                found.extend(_walk_json_ld(payload[key]))
        if "item" in payload:
            found.extend(_walk_json_ld(payload["item"]))
    return found


def _job_from_schema_org(node: dict[str, Any], base_url: str) -> RawJob | None:
    title = clean_text(node.get("title") or node.get("name"))
    if not title:
        return None

    url = node.get("url") or node.get("sameAs") or base_url
    return RawJob(
        title=title,
        url=absolutize(url, base_url),
        external_id=str(node.get("identifier", {}).get("value"))
        if isinstance(node.get("identifier"), dict)
        else _as_str(node.get("identifier")),
        location=_schema_location(node),
        description=strip_html(node.get("description")),
        employment_type=_as_str(node.get("employmentType")),
        posted_at=_as_str(node.get("datePosted")),
        salary=_schema_salary(node),
        remote="remote" if node.get("jobLocationType") == "TELECOMMUTE" else None,
        raw=node,
    )


def _schema_location(node: dict[str, Any]) -> str | None:
    location = node.get("jobLocation")
    if isinstance(location, list):
        location = location[0] if location else None
    if not isinstance(location, dict):
        return clean_text(_as_str(location))
    address = location.get("address")
    if not isinstance(address, dict):
        return clean_text(_as_str(address))
    parts = [
        address.get("addressLocality"),
        address.get("addressRegion"),
        address.get("addressCountry")
        if isinstance(address.get("addressCountry"), str)
        else (address.get("addressCountry") or {}).get("name"),
    ]
    return clean_text(", ".join(p for p in parts if isinstance(p, str) and p)) or None


def _schema_salary(node: dict[str, Any]) -> str | None:
    salary = node.get("baseSalary")
    if not isinstance(salary, dict):
        return None
    value = salary.get("value")
    currency = salary.get("currency", "")
    if isinstance(value, dict):
        low, high = value.get("minValue"), value.get("maxValue")
        single = value.get("value")
        if low or high:
            return f"{currency} {low or ''}-{high or ''}".strip()
        if single:
            return f"{currency} {single}".strip()
    return None


def _from_next_data(html: str, base_url: str) -> list[RawJob]:
    soup = BeautifulSoup(html, "lxml")
    tag = soup.find("script", attrs={"id": "__NEXT_DATA__"})
    if tag is None:
        return []
    payload = _safe_json(tag.string or tag.get_text() or "")
    if payload is None:
        return []
    return _harvest(payload, base_url)


def _from_state_blobs(html: str, base_url: str) -> list[RawJob]:
    for pattern in _STATE_PATTERNS:
        match = pattern.search(html)
        if not match:
            continue
        payload = _safe_json(match.group(1))
        if payload is None:
            continue
        jobs = _harvest(payload, base_url)
        if jobs:
            return jobs
    return []


# --- Generic harvesting ---------------------------------------------------


def _harvest(payload: Any, base_url: str, *, depth: int = 0) -> list[RawJob]:
    """Search a nested structure for the largest plausible list of postings.

    Depth-limited: framework state trees are deep and mostly irrelevant, and an
    unbounded walk over a 5 MB Next.js payload is slower than the render that
    produced it.

    Takes the *largest* qualifying list rather than the first, because these
    payloads routinely contain both a three-item "featured jobs" array and the
    full board, and the first one encountered is usually the short one.
    """
    if depth > 8:
        return []

    best: list[RawJob] = []

    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in _LIST_KEYS and isinstance(value, list):
                candidate = _jobs_from_list(value, base_url)
                if len(candidate) > len(best):
                    best = candidate
            nested = _harvest(value, base_url, depth=depth + 1)
            if len(nested) > len(best):
                best = nested
    elif isinstance(payload, list):
        candidate = _jobs_from_list(payload, base_url)
        if len(candidate) > len(best):
            best = candidate
        for item in payload[:50]:
            nested = _harvest(item, base_url, depth=depth + 1)
            if len(nested) > len(best):
                best = nested

    return best


def _jobs_from_list(items: list[Any], base_url: str) -> list[RawJob]:
    dicts = [item for item in items if isinstance(item, dict)]
    if not dicts:
        return []

    jobs: list[RawJob] = []
    for item in dicts:
        title = _first_value(item, _TITLE_KEYS)
        url = _first_value(item, _URL_KEYS)
        if not title or not url:
            continue
        jobs.append(
            RawJob(
                title=clean_text(title),
                url=absolutize(url, base_url),
                external_id=_as_str(_first_value(item, _ID_KEYS)),
                location=clean_text(_flatten_location(_first_value(item, _LOCATION_KEYS))),
                description=strip_html(_first_value(item, _DESCRIPTION_KEYS)),
                posted_at=_as_str(_first_value(item, _DATE_KEYS)),
                raw=item,
            )
        )

    # A list where only a fraction of entries look like postings is probably
    # not a job list at all — reject rather than contribute noise.
    return jobs if len(jobs) >= max(1, len(dicts) // 2) else []


def _first_value(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _flatten_location(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("name", "text", "displayName", "city", "locationName"):
            if isinstance(value.get(key), str):
                return value[key]
        parts = [value.get("city"), value.get("region"), value.get("country")]
        joined = ", ".join(p for p in parts if isinstance(p, str) and p)
        return joined or None
    if isinstance(value, list) and value:
        return _flatten_location(value[0])
    return None


def _as_str(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    return str(value)


def _safe_json(text: str) -> Any | None:
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def extract_job_detail(html: str) -> JobDetail | None:
    """Read one posting's detail from schema.org markup on its own page.

    Returns ``None`` rather than a guess when the page publishes no
    ``JobPosting``: a wrong description is worse than none, because the
    experience parser would then read requirements belonging to another role.
    """
    from app.scrapers.base import JobDetail

    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        payload = _safe_json(tag.string or tag.get_text() or "")
        if payload is None:
            continue
        for node in _walk_json_ld(payload):
            description = strip_html(node.get("description"))
            if not description:
                continue
            return JobDetail(
                description=description,
                requirements=strip_html(node.get("qualifications"))
                or strip_html(node.get("experienceRequirements")),
                employment_type=_as_str(node.get("employmentType")),
                posted_at=_as_str(node.get("datePosted")),
                raw={"source": "json-ld"},
            )
    return None
