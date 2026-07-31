"""Selector-driven extraction (ladder tiers 3 and 4).

Selectors are always resolved *relative to a repeating container*. Absolute
per-field selectors are the classic trap: they extract a perfectly plausible
list of ten titles and ten locations that belong to different postings once the
page has any conditional markup, and nothing downstream can detect the
mismatch. Anchoring each field inside its own container makes that failure
impossible by construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bs4 import BeautifulSoup, Tag

from app.core.logging import get_logger
from app.models.enums import SelectorStrategy
from app.scrapers.base import RawJob
from app.utils.text import clean_text
from app.utils.urls import absolutize

if TYPE_CHECKING:
    from app.models.selector import Selector

logger = get_logger(__name__)

#: Attributes worth reading when a field's element is not a text node.
_VALUE_ATTRIBUTES = ("datetime", "content", "title", "aria-label", "value")


@dataclass(slots=True)
class SelectorSet:
    """One extraction strategy, independent of where it came from."""

    container: str | None = None
    title: str | None = None
    url: str | None = None
    location: str | None = None
    description: str | None = None
    date: str | None = None
    department: str | None = None
    strategy: SelectorStrategy = SelectorStrategy.CSS
    requires_render: bool = False
    version: int | None = None

    @classmethod
    def from_model(cls, selector: Selector) -> SelectorSet:
        return cls(
            container=selector.container_selector,
            title=selector.title_selector,
            url=selector.url_selector,
            location=selector.location_selector,
            description=selector.description_selector,
            date=selector.date_selector,
            department=selector.department_selector,
            strategy=selector.strategy,
            requires_render=selector.requires_render,
            version=selector.selector_version,
        )

    def is_usable(self) -> bool:
        """A container plus a title is the minimum viable strategy.

        Without a container there is nothing to iterate; without a title there
        is nothing worth extracting. Everything else is optional enrichment.
        """
        return bool(self.container and self.title)


def extract_with_selectors(
    html: str, base_url: str, selectors: SelectorSet
) -> list[RawJob]:
    if not selectors.is_usable():
        return []
    if selectors.strategy is SelectorStrategy.XPATH:
        return _extract_xpath(html, base_url, selectors)
    return _extract_css(html, base_url, selectors)


# --- CSS ------------------------------------------------------------------


#: Elements that exist only for screen readers. They carry labels like
#: "Location" or "Actions" that sit inside the very nodes a location selector
#: targets, so without removing them a field reads "Location Bengaluru".
_SCREEN_READER_ONLY = ".a11y, .sr-only, .visually-hidden, .screen-reader-text, .visuallyhidden"


def _extract_css(html: str, base_url: str, selectors: SelectorSet) -> list[RawJob]:
    soup = BeautifulSoup(html, "lxml")
    for hidden in soup.select(_SCREEN_READER_ONLY):
        hidden.decompose()
    try:
        containers = soup.select(selectors.container or "")
    except Exception as exc:
        # soupsieve raises on malformed selectors, which is a real possibility
        # for LLM-generated ones. A bad selector is an empty result, not a
        # crash that takes the whole scan down.
        logger.warning("selector.invalid_css", selector=selectors.container, error=str(exc))
        return []

    jobs: list[RawJob] = []
    for container in containers:
        title = _css_text(container, selectors.title)
        if not title:
            continue
        jobs.append(
            RawJob(
                title=title,
                url=_css_url(container, selectors.url, base_url),
                location=_css_text(container, selectors.location),
                description=_css_text(container, selectors.description),
                department=_css_text(container, selectors.department),
                posted_at=_css_text(container, selectors.date),
                raw={"html": str(container)[:4000]},
            )
        )
    return _collapse_by_url(jobs)


def _collapse_by_url(jobs: list[RawJob]) -> list[RawJob]:
    """Two extractions pointing at the same posting are one posting.

    Sites frequently emit several sibling elements per job that all satisfy a
    reasonable container selector — Apple renders a title row, a
    "See full role description" links row and a spacer, each carrying the same
    class. Tightening the selector per site does not generalise; collapsing on
    the URL does, because the link is what identifies the posting.

    The richest entry wins rather than the first: the title row has the real
    title and the date, while the sibling rows have a link and little else.
    """
    best: dict[str, RawJob] = {}
    without_url: list[RawJob] = []

    for job in jobs:
        if not job.url:
            without_url.append(job)
            continue
        existing = best.get(job.url)
        if existing is None or _field_count(job) > _field_count(existing):
            best[job.url] = job

    return [*best.values(), *without_url]


def _field_count(job: RawJob) -> int:
    return sum(
        1
        for value in (job.title, job.location, job.posted_at, job.department, job.description)
        if value
    )


#: Pseudo-selector meaning "the container element itself". Needed because the
#: most common minimal listing markup is a bare ``<a>`` per posting, where the
#: title, the link, and the container are all the same node and any descendant
#: selector would match nothing.
SELF = "self"


def _split_alternatives(selector: str) -> list[str]:
    """Split ``"h3 a, a"`` into ordered alternatives, ignoring commas nested
    inside ``:is(...)`` / ``:not(...)``."""
    parts, depth, current = [], 0, []
    for char in selector:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    parts.append("".join(current).strip())
    return [part for part in parts if part]


def _css_first(container: Tag, selector: str | None) -> Tag | None:
    """First match, trying comma-separated alternatives **in order**.

    ``select_one("h3 a, a")`` returns whichever matches first in *document*
    order, not the first selector that matches — so a fallback chain silently
    stops being a fallback chain. On a card containing both a heading link and
    a "See full role description" link, the wrong one wins. Trying each
    alternative separately makes the ordering mean what it looks like it means.
    """
    if not selector:
        return None
    if selector == SELF:
        return container
    for alternative in _split_alternatives(selector):
        try:
            found = container.select_one(alternative)
        except Exception:
            continue
        if found is not None:
            return found
    return None


def _css_text(container: Tag, selector: str | None) -> str | None:
    """Text for a field, falling back to the container itself.

    The empty-selector fallback is load-bearing: a very common board layout is
    ``<a class="job">Senior Engineer</a>``, where the title *is* the container
    and a nested title selector would find nothing.
    """
    if not selector:
        return None
    element = _css_first(container, selector)
    if element is None:
        return None
    text = clean_text(element.get_text(" ", strip=True))
    if text:
        return text
    for attribute in _VALUE_ATTRIBUTES:
        value = element.get(attribute)
        if isinstance(value, str) and value.strip():
            return clean_text(value)
    return None


def _css_url(container: Tag, selector: str | None, base_url: str) -> str | None:
    element = _css_first(container, selector)
    if element is None:
        # Fall back to the container's own href, then any link inside it —
        # most listings wrap the whole card in the anchor.
        element = container if container.name == "a" else container.find("a")
    if element is None or not isinstance(element, Tag):
        return None
    href = element.get("href")
    if not isinstance(href, str):
        return None
    return absolutize(href, base_url)


# --- XPath ----------------------------------------------------------------


def _extract_xpath(html: str, base_url: str, selectors: SelectorSet) -> list[RawJob]:
    from lxml import html as lxml_html

    try:
        tree = lxml_html.fromstring(html)
        containers = tree.xpath(selectors.container or "")
    except Exception as exc:
        logger.warning("selector.invalid_xpath", selector=selectors.container, error=str(exc))
        return []

    jobs: list[RawJob] = []
    for container in containers:
        title = _xpath_text(container, selectors.title)
        if not title:
            continue
        jobs.append(
            RawJob(
                title=title,
                url=_xpath_url(container, selectors.url, base_url),
                location=_xpath_text(container, selectors.location),
                description=_xpath_text(container, selectors.description),
                department=_xpath_text(container, selectors.department),
                posted_at=_xpath_text(container, selectors.date),
                raw={},
            )
        )
    return jobs


def _xpath_eval(container: Any, expression: str | None) -> list[Any]:
    if not expression:
        return []
    # Relative expressions must start with "." or lxml silently evaluates them
    # against the document root and every container yields the same value.
    if expression.startswith("/"):
        expression = f".{expression}"
    try:
        return list(container.xpath(expression))
    except Exception:
        return []


def _xpath_text(container: Any, expression: str | None) -> str | None:
    for node in _xpath_eval(container, expression):
        # lxml yields bare strings for @attr expressions and elements otherwise.
        text = clean_text(node if isinstance(node, str) else node.text_content())
        if text:
            return text
    return None


def _xpath_url(container: Any, expression: str | None, base_url: str) -> str | None:
    nodes = _xpath_eval(container, expression) or _xpath_eval(container, ".//a/@href")
    for node in nodes:
        href = node if isinstance(node, str) else node.get("href")
        if href:
            return absolutize(href, base_url)
    return None
