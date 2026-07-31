"""HTML reduction for the LLM.

Never send a whole document to a model. A career page is routinely 500 KB of
markup — roughly 150 k tokens — of which the listing is maybe 2%. Sending all of
it is expensive, slower, and *less accurate*: the signal is buried in analytics
scripts, cookie banners, and inlined CSS.

This module answers "where is the list of jobs?" structurally, then emits a
skeleton of just that region. In practice it takes a 500 KB page down to under
10 KB while keeping every attribute a selector could reference.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, Comment, Tag

from app.core.logging import get_logger
from app.utils.text import truncate

logger = get_logger(__name__)

#: Elements that never contain a job listing and are pure token cost.
_DROP_TAGS = (
    "script", "style", "noscript", "svg", "path", "iframe", "canvas",
    "picture", "source", "video", "audio", "link", "meta", "template",
)

#: Landmarks that are structurally incapable of being the listing.
_DROP_ROLES = ("banner", "contentinfo", "navigation", "search", "complementary")

#: Attributes worth keeping — anything a CSS selector or XPath could target.
#: Everything else (inline styles, analytics hooks, framework internals) goes.
_KEEP_ATTRS = ("class", "id", "href", "datetime", "role", "aria-label", "itemprop")

_JOB_HREF_RE = re.compile(
    r"/(job|jobs|career|careers|position|positions|opening|openings|vacancy|"
    r"vacancies|requisition|posting|details)[/?#-]",
    re.IGNORECASE,
)

_LISTING_HINT_RE = re.compile(
    r"job|position|opening|vacancy|posting|career|role|opportunit", re.IGNORECASE
)


@dataclass(slots=True)
class ReducedHtml:
    """A candidate listing region, ready to hand to a model."""

    html: str
    #: CSS path of the region within the original document, so the model's
    #: selectors can be interpreted (and validated) against the right scope.
    root_path: str
    candidate_count: int
    original_bytes: int
    reduced_bytes: int

    @property
    def reduction_ratio(self) -> float:
        return 1 - (self.reduced_bytes / self.original_bytes) if self.original_bytes else 0.0


def reduce_html(html: str, *, max_chars: int = 24_000, max_items: int = 12) -> ReducedHtml:
    """Find the listing region and return a compact skeleton of it.

    Only the first ``max_items`` repeated entries are kept. A model needs a
    handful of examples to infer the pattern; the other 480 rows on the page
    add cost and nothing else.
    """
    original_bytes = len(html)
    soup = BeautifulSoup(html, "lxml")
    _strip_noise(soup)

    container = _best_container(soup)
    if container is None:
        # No repeating structure found. Fall back to the largest text-bearing
        # region so the model still sees *something* real, rather than an
        # arbitrary prefix of the document that may be all <head>.
        container = soup.body or soup
        logger.debug("html_reducer.no_container")

    children = _repeating_children(container)
    if children and len(children) > max_items:
        for extra in children[max_items:]:
            extra.decompose()

    _prune_attributes(container)
    rendered = truncate(str(container), max_chars, suffix="\n<!-- truncated -->")

    return ReducedHtml(
        html=rendered,
        root_path=_css_path(container),
        candidate_count=len(children),
        original_bytes=original_bytes,
        reduced_bytes=len(rendered),
    )


# --- Steps ----------------------------------------------------------------


def _strip_noise(soup: BeautifulSoup) -> None:
    for tag_name in _DROP_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()
    for tag in soup.find_all(attrs={"role": True}):
        if tag.get("role") in _DROP_ROLES:
            tag.decompose()
    for tag_name in ("nav", "footer", "header"):
        for tag in soup.find_all(tag_name):
            # A <header> can legitimately wrap a listing on minimal sites, so
            # only drop it when it contains no job-shaped links.
            if not _count_job_links(tag):
                tag.decompose()
    # Build-tool comments can run to kilobytes and carry no extractable signal.
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()


def _best_container(soup: BeautifulSoup) -> Tag | None:
    """Score every element and return the most listing-like.

    The heuristic that carries the most weight is *sibling similarity*: a job
    list is n children with the same tag and overlapping classes, each holding
    exactly one job-shaped link. That pattern is rare in page chrome and almost
    universal in listings.
    """
    best: Tag | None = None
    best_score = 0.0

    for element in soup.find_all(["ul", "ol", "div", "section", "table", "tbody", "main"]):
        children = _repeating_children(element)
        if len(children) < 2:
            continue

        job_links = _count_job_links(element)
        if job_links < 2:
            continue

        # One link per row is the signature of a listing; many links per row
        # usually means we are looking at a page-level wrapper.
        links_per_child = job_links / len(children)
        density = 1.0 if 0.5 <= links_per_child <= 2.0 else 0.4

        similarity = _sibling_similarity(children)
        hint = 1.2 if _LISTING_HINT_RE.search(" ".join(_signature_tokens(element))) else 1.0

        score = len(children) * similarity * density * hint

        # Prefer the innermost qualifying element: an ancestor scores similarly
        # but drags in the whole page around the list.
        if score > best_score:
            best_score = score
            best = element

    return best


def _repeating_children(element: Tag) -> list[Tag]:
    """The children that form the repeating pattern, if there is one."""
    children = [child for child in element.find_all(recursive=False) if isinstance(child, Tag)]
    if len(children) < 2:
        return []
    tags = [child.name for child in children]
    dominant = max(set(tags), key=tags.count)
    if tags.count(dominant) < max(2, len(children) // 2):
        return []
    return [child for child in children if child.name == dominant]


def _sibling_similarity(children: list[Tag]) -> float:
    """Fraction of children sharing the most common class signature."""
    if not children:
        return 0.0
    signatures = [" ".join(sorted(child.get("class") or [])) for child in children]
    dominant = max(set(signatures), key=signatures.count)
    return signatures.count(dominant) / len(signatures)


def _count_job_links(element: Tag) -> int:
    count = 0
    for anchor in element.find_all("a", href=True):
        href = anchor.get("href")
        if isinstance(href, str) and _JOB_HREF_RE.search(href):
            count += 1
    return count


def _signature_tokens(element: Tag) -> list[str]:
    tokens = list(element.get("class") or [])
    if element.get("id"):
        tokens.append(str(element.get("id")))
    for attr in ("data-testid", "data-test", "aria-label"):
        if element.get(attr):
            tokens.append(str(element.get(attr)))
    return tokens


def _prune_attributes(root: Tag) -> None:
    for element in [root, *root.find_all(True)]:
        if not isinstance(element, Tag):
            continue
        kept = {}
        for name, value in list(element.attrs.items()):
            if name in _KEEP_ATTRS or name.startswith("data-test"):
                # Long class lists (Tailwind, CSS modules) are mostly noise;
                # the first few carry the semantic names a selector would use.
                if name == "class" and isinstance(value, list):
                    value = value[:6]
                kept[name] = value
        element.attrs = kept


def _css_path(element: Tag) -> str:
    """Short, human-readable path to the region. Diagnostic, not executable."""
    parts: list[str] = []
    node: Tag | None = element
    while node is not None and node.name not in ("[document]", "html") and len(parts) < 5:
        segment = node.name
        if node.get("id"):
            segment += f"#{node['id']}"
            parts.append(segment)
            break
        classes = node.get("class") or []
        if classes:
            segment += "." + ".".join(classes[:2])
        parts.append(segment)
        node = node.parent if isinstance(node.parent, Tag) else None
    return " > ".join(reversed(parts))
