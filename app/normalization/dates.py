"""Posting-date parsing.

Career sites express "when was this posted" in every format imaginable:
ISO-8601, epoch milliseconds, "3 days ago", "Posted Yesterday", "July 28", and
localised month names. All of them have to become a single ``date`` so that
"new this week" is answerable.

Two rules shape the implementation:

* **Never guess wildly.** Returning ``None`` is strictly better than returning
  a wrong date — a wrong date silently corrupts every "recent postings" query,
  whereas a null is visible and ignorable.
* **Never return the future.** A bare "July 28" seen in January is last year's
  posting, not one six months ahead. Sites omit the year constantly and this is
  the single most common source of nonsense dates.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

from dateutil import parser as dateutil_parser

from app.utils.time import utcnow

_RELATIVE_RE = re.compile(
    r"(?P<count>\d+)\+?\s*(?P<unit>second|minute|hour|day|week|month|year)s?\s*ago",
    re.IGNORECASE,
)
_POSTED_PREFIX_RE = re.compile(
    r"^\s*(posted|published|listed|added|created|updated|date)\s*(on|:)?\s*",
    re.IGNORECASE,
)
_WITHIN_RE = re.compile(
    r"(in the last|within|past)\s+(?P<count>\d+)\s+(?P<unit>day|week|month)s?",
    re.IGNORECASE,
)

_UNIT_DAYS = {
    "second": 0.0,
    "minute": 0.0,
    "hour": 0.0,
    "day": 1.0,
    "week": 7.0,
    "month": 30.44,
    "year": 365.25,
}

#: Epoch values below this are almost certainly seconds, above are
#: milliseconds. 10^11 seconds is the year 5138; 10^11 ms is 1973.
_EPOCH_MS_THRESHOLD = 100_000_000_000


def parse_posted_date(value: str | None, *, today: date | None = None) -> date | None:
    """Best-effort conversion of a posting date to a calendar date."""
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    reference = today or utcnow().date()

    epoch = _parse_epoch(text)
    if epoch is not None:
        return epoch

    text = _POSTED_PREFIX_RE.sub("", text).strip()
    lowered = text.lower()

    if lowered in ("today", "just posted", "new", "just now", "now"):
        return reference
    if lowered == "yesterday":
        return reference - timedelta(days=1)

    relative = _RELATIVE_RE.search(lowered)
    if relative:
        days = int(relative.group("count")) * _UNIT_DAYS[relative.group("unit").lower()]
        return reference - timedelta(days=round(days))

    # "Posted within the last 30 days" is a bucket, not a date. The most recent
    # date consistent with the claim is today, and treating it as 30 days old
    # would wrongly age out a brand-new posting.
    within = _WITHIN_RE.search(lowered)
    if within:
        return reference

    return _parse_absolute(text, reference)


def _parse_epoch(text: str) -> date | None:
    if not text.isdigit():
        return None
    number = int(text)
    # Anything shorter than 9 digits is a job id, a page count, or a typo —
    # not a timestamp. Guarding here stops "12345" becoming 1970-01-01.
    if len(text) < 9:
        return None
    seconds = number / 1000 if number >= _EPOCH_MS_THRESHOLD else number
    try:
        # Interpreted in UTC, not the host's local zone: the same payload must
        # normalise to the same date on a developer's laptop and on a server.
        return datetime.fromtimestamp(seconds, tz=UTC).date()
    except (OverflowError, OSError, ValueError):
        return None


def _parse_absolute(text: str, reference: date) -> date | None:
    try:
        # ``default`` supplies the year for month/day-only strings; dateutil
        # would otherwise use the current year even when that lands in the
        # future.
        parsed = dateutil_parser.parse(
            text,
            fuzzy=True,
            default=datetime(reference.year, reference.month, reference.day),
        )
    except (ValueError, OverflowError, TypeError):
        return None

    result = parsed.date()

    if result > reference:
        # A parsed date in the future means the year was inferred wrongly.
        # Roll back one year if that produces a sane date; otherwise the input
        # was not a posting date at all.
        rolled = result.replace(year=result.year - 1)
        return rolled if rolled <= reference else None

    # Job boards do not carry postings from the 1990s; a date that old means
    # something numeric was mis-parsed.
    if result.year < reference.year - 5:
        return None

    return result


def days_since(value: date | None, *, today: date | None = None) -> int | None:
    if value is None:
        return None
    return ((today or utcnow().date()) - value).days


def parse_posted_datetime(value: str | None) -> datetime | None:
    """Full timestamp, or ``None`` when the source only carried a day.

    Separate from :func:`parse_posted_date` on purpose. That one always
    produces something usable by coercing relative phrases to a day; this one
    returns ``None`` unless the source genuinely carried a time, so a coarse
    input never masquerades as a precise one.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    if text.isdigit() and len(text) >= 9:
        number = int(text)
        seconds = number / 1000 if number >= _EPOCH_MS_THRESHOLD else number
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None

    # Only accept strings that actually look like they carry a clock time.
    if not re.search(r"\d{1,2}:\d{2}", text):
        return None
    try:
        parsed = dateutil_parser.parse(text)
    except (ValueError, OverflowError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed if parsed <= utcnow() else None


def humanize_age(
    moment: datetime | None, day: date | None = None, *, now: datetime | None = None
) -> str:
    """Render a posting age the way a person would say it.

    Prefers the precise timestamp and falls back to the date, so a board that
    publishes only a day still reads sensibly ("2 days ago") instead of
    pretending to an accuracy it never had.
    """
    reference = now or utcnow()

    if moment is not None:
        delta = reference - moment
        minutes = delta.total_seconds() / 60
        if minutes < 2:
            return "just now"
        if minutes < 60:
            return f"{int(minutes)} minutes ago"
        hours = minutes / 60
        if hours < 24:
            count = int(hours)
            return "1 hour ago" if count == 1 else f"{count} hours ago"
        day = moment.date()

    if day is None:
        return "—"

    days = (reference.date() - day).days
    if days < 0:
        return "just now"
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    if days < 30:
        weeks = days // 7
        return "1 week ago" if weeks == 1 else f"{weeks} weeks ago"
    months = days // 30
    return "1 month ago" if months == 1 else f"{months} months ago"
