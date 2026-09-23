from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Timezone-aware now.

    Wrapped rather than called inline so that (a) ``datetime.utcnow()`` — which
    returns a *naive* datetime and silently compares wrong against our
    ``timestamptz`` columns — never appears in the codebase, and (b) tests have
    a single place to freeze.
    """
    return datetime.now(UTC)


def as_aware(moment: datetime | None) -> datetime | None:
    """Attach UTC to a naive timestamp read back from the database.

    Postgres ``timestamptz`` round-trips as aware; SQLite has no timezone type
    and hands back naive datetimes for the same column. Arithmetic mixing the
    two raises, so any code that compares a stored timestamp against
    :func:`utcnow` has to normalise first or it works on one backend and
    crashes on the other.
    """
    if moment is None or moment.tzinfo is not None:
        return moment
    return moment.replace(tzinfo=UTC)


def elapsed_ms(start: datetime, end: datetime | None = None) -> int:
    return int(((end or utcnow()) - start).total_seconds() * 1000)
