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


def elapsed_ms(start: datetime, end: datetime | None = None) -> int:
    return int(((end or utcnow()) - start).total_seconds() * 1000)
