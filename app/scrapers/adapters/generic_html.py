"""Fallback adapter for pages that match no known platform.

Contributes no platform knowledge by definition — its whole value is being a
valid :class:`BaseScraper` so that an unrecognised site travels the same code
path as a recognised one. The extraction ladder supplies the intelligence:
embedded JSON, then learned selectors for this domain, then the LLM learner.

It does carry a small set of structural heuristics, because a meaningful share
of hand-built career pages are a ``<ul>`` of links under a "Open positions"
heading, and recognising that costs nothing and saves an LLM call.
"""

from __future__ import annotations

from typing import ClassVar

from app.extractors.selector_extractor import SelectorSet
from app.models.enums import ATSType, ScrapingStrategy
from app.scrapers.adapters.html_base import HtmlListingScraper


class GenericHtmlScraper(HtmlListingScraper):
    ats_type: ClassVar[ATSType] = ATSType.GENERIC_HTML
    default_strategy: ClassVar[ScrapingStrategy] = ScrapingStrategy.AUTO

    builtin_selectors: ClassVar[tuple[SelectorSet, ...]] = (
        # Semantic markup, when we are lucky.
        SelectorSet(
            container="li[class*='job'], li[class*='position'], li[class*='vacancy']",
            title="a, h2, h3",
            url="a",
            location="[class*='location']",
            date="time, [class*='date']",
        ),
        # Card layouts: a repeated <div>/<article> per posting. Extremely
        # common on hand-built career pages (Apple's board is one), and
        # invisible to the <li>/<tr> patterns above.
        SelectorSet(
            container="div[class*='job-list-item'], div[class*='job-card'], "
            "div[class*='job-result'], div[class*='job-tile'], "
            "article[class*='job'], div[class*='opening-item']",
            title="h2 a, h3 a, h4 a, a[class*='title'], h2, h3, a",
            url="h2 a, h3 a, h4 a, a",
            location="[class*='location-sub'], [class*='location'], [class*='city']",
            date="[class*='posted'], [class*='date'], time",
            department="[class*='team'], [class*='department'], [class*='category']",
        ),
        SelectorSet(
            container="tr[class*='job'], tbody tr:has(a[href*='job'])",
            title="td:first-child a, a",
            url="a",
            location="td:nth-of-type(2)",
            date="td:nth-of-type(3)",
        ),
        # Last resort: any anchor whose href looks like a posting. Broad enough
        # to catch navigation chrome too, which is exactly why the validation
        # scorer — not this selector — decides whether the result is trusted.
        SelectorSet(
            container="a[href*='/job/'], a[href*='/jobs/'], a[href*='/careers/'], "
            "a[href*='/position'], a[href*='/vacancy'], a[href*='/details/'], "
            "a[href*='/opening']",
            title="self",
            url="self",
        ),
    )
