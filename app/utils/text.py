"""Text helpers used by normalisation, matching, and hashing."""

from __future__ import annotations

import html
import re
import unicodedata

_WHITESPACE = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")
_BLOCK_END = re.compile(r"</(p|div|li|br|tr|h[1-6])\s*>", re.IGNORECASE)


def clean_text(value: str | None) -> str:
    """Collapse whitespace, unescape entities, normalise unicode.

    NFKC folds the non-breaking spaces and typographic dashes that boards emit
    into their ASCII equivalents — without it, ``Berlin,\xa0Germany`` and
    ``Berlin, Germany`` hash differently and the same posting notifies twice.
    """
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", html.unescape(value))
    return _WHITESPACE.sub(" ", text).strip()


def strip_html(markup: str | None) -> str:
    """Turn a markup fragment into readable plain text.

    Entities are decoded *before* tags are stripped, and that order is
    load-bearing. Several ATS APIs — Greenhouse among them — return HTML that
    has itself been HTML-escaped, so the payload contains ``&lt;strong&gt;``
    rather than ``<strong>``. Stripping first sees no tags at all, and the
    later unescape then resurrects them as literal text in the description.

    Block-level boundaries become newlines so list items and paragraphs do not
    run together into one unreadable line. For whole documents prefer
    ``extractors.html_reducer``, which understands structure.
    """
    if not markup:
        return ""

    text = html.unescape(markup)
    # A second pass for the doubly-escaped case; bounded, so a string full of
    # literal ampersands cannot loop.
    if "&lt;" in text or "&gt;" in text:
        text = html.unescape(text)

    text = _BLOCK_END.sub("\n", text)
    text = _TAG.sub(" ", text)

    # Collapse per line rather than globally, so the paragraph breaks survive.
    lines = [_WHITESPACE.sub(" ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def normalize_key(value: str | None) -> str:
    """Aggressive fold used for equality checks and hashing.

    Lowercases, strips accents, and removes everything but alphanumerics and
    single spaces, so ``Sr. Software Engineer (m/f/d)`` and
    ``sr software engineer m f d`` compare equal.
    """
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value.lower())
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _WHITESPACE.sub(" ", re.sub(r"[^a-z0-9]+", " ", ascii_only)).strip()


def contains_keyword(haystack: str, keyword: str) -> bool:
    """Word-boundary containment.

    Substring matching is wrong here in a way that bites: an excluded keyword
    of ``"lead"`` would otherwise veto every posting mentioning "leadership",
    and ``"go"`` would veto "Django". Multi-word keywords are matched as a
    phrase on the normalised text.
    """
    if not haystack or not keyword:
        return False
    target = normalize_key(keyword)
    if not target:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(target)}(?![a-z0-9])",
                     normalize_key(haystack)) is not None


def find_keywords(haystack: str, keywords: list[str]) -> list[str]:
    """Return the subset of ``keywords`` present in ``haystack``.

    Normalises the haystack once rather than once per keyword — the difference
    is measurable when scoring a 200-skill profile against a 500-job board.
    """
    if not haystack:
        return []
    normalized = normalize_key(haystack)
    found = []
    for keyword in keywords:
        target = normalize_key(keyword)
        if target and re.search(
            rf"(?<![a-z0-9]){re.escape(target)}(?![a-z0-9])", normalized
        ):
            found.append(keyword)
    return found


def truncate(value: str, limit: int, suffix: str = "…") -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - len(suffix))].rstrip() + suffix
