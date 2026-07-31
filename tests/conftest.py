"""Shared test fixtures.

Unit tests run against in-memory SQLite. That is possible only because the
models declare Postgres types with SQLite variants (see
``app/database/types.py``); without it, every test touching the ORM would need
a live Postgres and the suite would stop being something you run on every save.

Tests that genuinely need Postgres behaviour — the ``ON CONFLICT`` insert path
in particular — are marked ``integration`` and skipped by default.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

# Set before any application import so the settings singleton never picks up a
# developer's real .env and tries to reach a real database.
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("LLM_PROVIDER", "null")
os.environ.setdefault("LLM_ENABLED", "false")
os.environ.setdefault("SCRAPE_RESPECT_ROBOTS", "false")
os.environ.setdefault("MATCH_SEMANTIC_ENABLED", "false")
os.environ.setdefault("ENRICH_DETAILS", "false")

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import get_settings
from app.models import Base
from app.models.company import Company
from app.models.enums import (
    ATSType,
    RemotePreference,
    ScrapeFrequency,
    ScrapingStrategy,
    SeniorityLevel,
)
from app.models.user import User, UserProfile


@pytest.fixture(scope="session", autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def engine() -> Iterator[sa.Engine]:
    """A fresh in-memory database per test.

    ``StaticPool`` plus a shared connection is required: SQLite's in-memory
    database is per-connection, so without it each checkout from the pool would
    see an empty schema.
    """
    engine = sa.create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def db_session(engine: sa.Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    session = factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


# --- Domain fixtures ------------------------------------------------------


@pytest.fixture
def company(db_session: Session) -> Company:
    entity = Company(
        name="Acme Corp",
        career_url="https://boards.greenhouse.io/acme",
        website="greenhouse.io",
        ats_type=ATSType.GREENHOUSE,
        scraping_strategy=ScrapingStrategy.API,
        board_token="acme",
        scrape_frequency=ScrapeFrequency.DAILY,
        scrape_interval_minutes=1440,
    )
    db_session.add(entity)
    db_session.commit()
    return entity


@pytest.fixture
def generic_company(db_session: Session) -> Company:
    entity = Company(
        name="Widgets Ltd",
        career_url="https://widgets.example.com/careers",
        website="example.com",
        ats_type=ATSType.GENERIC_HTML,
        scraping_strategy=ScrapingStrategy.HTTP,
        scrape_frequency=ScrapeFrequency.DAILY,
        scrape_interval_minutes=1440,
    )
    db_session.add(entity)
    db_session.commit()
    return entity


@pytest.fixture
def user(db_session: Session) -> User:
    entity = User(email="candidate@example.com", full_name="Test Candidate")
    db_session.add(entity)
    db_session.commit()
    return entity


@pytest.fixture
def profile(db_session: Session, user: User) -> UserProfile:
    entity = UserProfile(
        user_id=user.id,
        preferred_roles=["Software Engineer", "Backend Engineer"],
        preferred_locations=["Bangalore", "Bengaluru", "Remote"],
        preferred_skills=["Python", "Java", "AWS", "Docker", "SQL", "React"],
        excluded_keywords=["Sales", "Unpaid"],
        seniority=SeniorityLevel.MID,
        remote_preference=RemotePreference.ANY,
        years_experience=3,
        match_threshold=0.6,
    )
    db_session.add(entity)
    db_session.commit()
    return entity
