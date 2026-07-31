"""Client-rendered careers pages built on React/Vue/Angular.

Not an ATS — a shape. These pages have no listing in their served HTML, so the
one thing this adapter contributes over the generic one is *forcing a render*
rather than letting the fetcher's heuristic decide.

Beyond that it is deliberately empty of extraction logic. There is no
"React selector"; whatever markup the framework produced is site-specific, and
finding it is the extraction ladder's job (embedded JSON first, then learned
selectors, then the LLM).
"""

from __future__ import annotations

from typing import ClassVar

from app.extractors.selector_extractor import SelectorSet
from app.models.enums import ATSType, ScrapingStrategy
from app.scrapers.adapters.html_base import HtmlListingScraper


class CustomReactScraper(HtmlListingScraper):
    ats_type: ClassVar[ATSType] = ATSType.CUSTOM_REACT
    default_strategy: ClassVar[ScrapingStrategy] = ScrapingStrategy.PLAYWRIGHT
    body_markers: ClassVar[tuple[str, ...]] = (
        "__next_data__",
        "data-reactroot",
        "id=\"__nuxt\"",
        "ng-version",
        "__nuxt__",
    )

    #: Conventional markup that a surprising number of hand-built career pages
    #: converge on. Cheap to try; when they miss, the ladder takes over.
    builtin_selectors: ClassVar[tuple[SelectorSet, ...]] = (
        SelectorSet(
            container="[data-testid*='job'], [data-test*='job'], [class*='job-card']",
            title="[class*='title'], h2, h3, a",
            url="a",
            location="[class*='location']",
            date="time, [class*='date']",
        ),
        SelectorSet(
            container="li[class*='opening'], div[class*='opening'], article[class*='job']",
            title="h2, h3, a",
            url="a",
            location="[class*='location'], [class*='office']",
            date="time",
        ),
    )

    def wait_for_selector(self) -> str | None:
        """Wait for *any* link that looks like a posting.

        A ``networkidle`` wait alone is not enough on pages that poll or hold a
        websocket open; waiting for a plausible job link is a far better proxy
        for "the listing has rendered".
        """
        return "a[href*='job'], a[href*='career'], a[href*='position'], a[href*='opening']"
