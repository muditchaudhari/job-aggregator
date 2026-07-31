"""Engine and session management.

A single synchronous engine serves both the API (via the threadpool, see AD-2)
and the Celery workers. The engine is created lazily so that importing this
module never opens a socket — which matters for unit tests and for ``--help``
style CLI invocations.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        url = settings.database_url
        kwargs: dict[str, object] = {
            "echo": settings.database_echo,
            "future": True,
        }
        if url.startswith("sqlite"):
            # SQLite has no server to pool connections to, and no idle timeout
            # to pre-ping against. What it does have is a single writer lock,
            # so a parallel scan needs a busy timeout or the second thread to
            # reach a write fails outright with "database is locked".
            kwargs["connect_args"] = {"timeout": 60, "check_same_thread": False}
        else:
            kwargs["pool_size"] = settings.database_pool_size
            kwargs["max_overflow"] = settings.database_max_overflow
            # Long-lived worker processes outlive the database's idle timeout;
            # without this they hand out sockets the server has already closed.
            kwargs["pool_pre_ping"] = True
            kwargs["pool_recycle"] = 1800
        _engine = create_engine(url, **kwargs)  # type: ignore[arg-type]
        if url.startswith("sqlite"):
            _enable_sqlite_wal(_engine)
    return _engine


def _enable_sqlite_wal(engine: Engine) -> None:
    """Let readers proceed while a writer holds the lock.

    The default rollback journal blocks readers for the duration of every
    write, which on a parallel scan means threads queueing behind each other
    for work they could have done concurrently.
    """

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


def ensure_schema() -> None:
    """Create any missing tables.

    Alembic owns schema *changes*; this covers the one case Alembic cannot,
    which is an empty database on a host that has no migration history to
    stamp — a cron runner restoring a cache that missed. Existing tables are
    left alone, so it is a no-op on a migrated database.
    """
    from app.models import Base

    engine = get_engine()
    existing = set(inspect(engine).get_table_names())
    missing = [t for name, t in Base.metadata.tables.items() if name not in existing]
    if missing:
        Base.metadata.create_all(engine, tables=missing)


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(),
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for background work.

    Commits on clean exit, rolls back on any exception. Celery tasks use this;
    request handlers use :func:`get_db` instead so FastAPI owns the lifecycle.
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency.

    Does not commit: endpoints that mutate state commit explicitly, which keeps
    the transaction boundary visible at the call site rather than implied by
    whether the response was a 2xx.
    """
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def check_database() -> bool:
    """Readiness probe."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def reset_engine() -> None:
    """Drop cached engine/session factory. Test-support only."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
