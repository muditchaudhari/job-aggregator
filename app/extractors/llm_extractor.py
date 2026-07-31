"""LLM extraction (ladder tier 5).

Two distinct jobs, and the difference matters economically:

* :func:`generate_selectors` asks the model for a *reusable strategy*. One call
  buys unlimited future scrapes of that domain at tier 3. This is the path we
  want.
* :func:`extract_fields_directly` asks the model for *this page's postings*.
  One call buys one scrape. Only used when selector generation itself fails,
  so that a site is not lost entirely while the learner keeps trying.

Both operate on reduced HTML, never on a whole document.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.config import get_settings
from app.core.errors import LLMResponseError
from app.core.logging import get_logger
from app.extractors.html_reducer import ReducedHtml, reduce_html
from app.extractors.selector_extractor import SelectorSet
from app.llm.client import LLMClient, UsageTally
from app.llm.prompts import (
    FIELD_EXTRACTION_SYSTEM,
    FIELD_EXTRACTION_USER,
    SELECTOR_SYSTEM,
    SELECTOR_USER,
)
from app.models.enums import SelectorStrategy
from app.scrapers.base import RawJob
from app.utils.text import clean_text
from app.utils.urls import absolutize

logger = get_logger(__name__)


@dataclass(slots=True)
class GeneratedSelectors:
    selectors: SelectorSet
    #: The model's own confidence. Treated as a hint only — the authoritative
    #: number comes from actually running the selectors and scoring the result.
    claimed_confidence: float
    notes: str | None
    model: str


def generate_selectors(
    html: str,
    url: str,
    *,
    client: LLMClient,
    tally: UsageTally | None = None,
    max_chars: int | None = None,
) -> GeneratedSelectors:
    """Ask the model for a reusable selector strategy for this site."""
    reduced: ReducedHtml = reduce_html(
        html, max_chars=max_chars or get_settings().llm_max_input_chars
    )
    logger.info(
        "llm_extractor.reducing",
        url=url,
        original_bytes=reduced.original_bytes,
        reduced_bytes=reduced.reduced_bytes,
        ratio=round(reduced.reduction_ratio, 3),
        candidates=reduced.candidate_count,
    )

    response = client.complete(
        system=SELECTOR_SYSTEM,
        prompt=SELECTOR_USER.format(
            url=url,
            root_path=reduced.root_path or "unknown",
            candidate_count=reduced.candidate_count,
            html=reduced.html,
        ),
        purpose="selector_generation",
        tally=tally,
    )

    payload = response.json()
    if not isinstance(payload, dict):
        raise LLMResponseError("selector response was not an object")

    container = _as_selector(payload.get("container_selector"))
    title = _as_selector(payload.get("title_selector"))
    if not container or not title:
        raise LLMResponseError(
            "model returned no container or title selector",
            container=container,
            title=title,
        )

    selectors = SelectorSet(
        container=container,
        title=title,
        url=_as_selector(payload.get("url_selector")),
        location=_as_selector(payload.get("location_selector")),
        description=_as_selector(payload.get("description_selector")),
        date=_as_selector(payload.get("date_selector")),
        department=_as_selector(payload.get("department_selector")),
        strategy=SelectorStrategy.CSS,
        requires_render=bool(payload.get("requires_render")),
    )

    return GeneratedSelectors(
        selectors=selectors,
        claimed_confidence=_as_confidence(payload.get("confidence")),
        notes=clean_text(payload.get("notes")) or None,
        model=response.model,
    )


def extract_fields_directly(
    html: str,
    url: str,
    *,
    client: LLMClient,
    tally: UsageTally | None = None,
) -> list[RawJob]:
    """One-shot extraction of this page's postings. No reusable artefact."""
    reduced = reduce_html(
        html, max_chars=get_settings().llm_max_input_chars, max_items=40
    )

    response = client.complete(
        system=FIELD_EXTRACTION_SYSTEM,
        prompt=FIELD_EXTRACTION_USER.format(url=url, html=reduced.html),
        purpose="field_extraction",
        tally=tally,
    )

    payload = response.json()
    entries = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise LLMResponseError("field extraction response had no 'jobs' array")

    jobs: list[RawJob] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        title = clean_text(entry.get("title"))
        if not title:
            continue
        jobs.append(
            RawJob(
                title=title,
                url=absolutize(entry.get("url"), url),
                location=clean_text(entry.get("location")) or None,
                department=clean_text(entry.get("department")) or None,
                employment_type=clean_text(entry.get("employment_type")) or None,
                posted_at=clean_text(entry.get("posted_at")) or None,
                salary=clean_text(entry.get("salary")) or None,
                raw={"source": "llm_field_extraction"},
            )
        )
    return jobs


# --- Response sanitising --------------------------------------------------


def _as_selector(value: Any) -> str | None:
    """Coerce a model-supplied selector to something safe to store.

    Models occasionally return ``"null"`` as a string, an empty string, or a
    list of alternatives. Storing any of those would produce a selector row
    that silently matches nothing on every future scrape.
    """
    if value is None:
        return None
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or cleaned.lower() in ("null", "none", "n/a"):
        return None
    return cleaned


def _as_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, confidence))
