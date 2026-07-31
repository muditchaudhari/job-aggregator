"""Application configuration.

Every tunable in the system is declared here and sourced from the environment.
Nothing else in the codebase reads ``os.environ`` directly.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    TEST = "test"


class Settings(BaseSettings):
    """Flat settings object, grouped by concern.

    List-valued options are declared as ``str`` and exposed through a parsed
    property. pydantic-settings otherwise insists on JSON syntax for ``list``
    fields sourced from the environment, which makes for hostile ``.env`` files
    (``["a","b"]`` instead of ``a,b``).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- General ----------------------------------------------------------
    environment: Environment = Environment.DEVELOPMENT
    debug: bool = True
    log_level: str = "INFO"
    log_json: bool = False
    secret_key: str = "change-me-in-production"
    api_prefix: str = "/api/v1"

    # --- Database ---------------------------------------------------------
    database_url: str = "postgresql+psycopg://jobs:jobs@localhost:5432/jobs"
    database_pool_size: int = 10
    database_max_overflow: int = 20
    database_echo: bool = False

    # --- Redis / Celery ---------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    # --- Scraping ---------------------------------------------------------
    scrape_http_timeout_seconds: float = 20.0
    scrape_render_timeout_seconds: float = 45.0
    scrape_max_retries: int = 3
    scrape_backoff_base_seconds: float = 2.0
    scrape_max_concurrency: int = 8
    scrape_respect_robots: bool = True
    scrape_requests_per_minute_per_domain: int = 20
    scrape_rotate_user_agents: bool = True
    scrape_proxies: str = ""
    scrape_max_consecutive_failures: int = 10

    # --- Playwright -------------------------------------------------------
    playwright_headless: bool = True
    playwright_browser: Literal["chromium", "firefox", "webkit"] = "chromium"
    playwright_wait_until: Literal["load", "domcontentloaded", "networkidle"] = "networkidle"
    playwright_viewport_width: int = 1440
    playwright_viewport_height: int = 900
    playwright_block_resources: str = "image,media,font"

    # --- Extraction -------------------------------------------------------
    extraction_min_confidence: float = 0.6
    extraction_min_jobs_expected: int = 1
    selector_regenerate_after_failures: int = 3
    selector_max_versions_retained: int = 10

    # --- LLM --------------------------------------------------------------
    # Gemini by default: it is the only one of the three with a genuinely free
    # tier, and its free tier is bounded by requests-per-day rather than by
    # spend — which suits a system whose model use is a rare failure path.
    llm_provider: Literal["openai", "anthropic", "gemini", "null"] = "gemini"
    #: Not a 2.5-series model: Google gates those to pre-existing users ahead of
    #: their shutdown, so a new key gets a 404 even though ``models.list()``
    #: still advertises them. ``make check-llm`` verifies callability, which is
    #: a stronger check than "appears in the listing".
    llm_model: str = "gemini-3-flash-preview"
    llm_enabled: bool = True
    llm_max_input_chars: int = 24_000
    llm_timeout_seconds: float = 60.0
    llm_max_output_tokens: int = 2000
    llm_temperature: float = 0.0
    llm_daily_budget_usd: float = 5.0
    llm_budget_breaker_enabled: bool = True

    openai_api_key: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""

    # --- Matching ---------------------------------------------------------
    match_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    match_semantic_enabled: bool = True
    match_semantic_max_jobs_per_run: int = 25

    #: Fetch each shortlisted posting's own page so experience requirements
    #: stated there (rather than in the listing) can be filtered on.
    enrich_details: bool = True
    #: One request per posting, so this is the knob that decides whether a
    #: scan takes seconds or minutes.
    max_details_per_run: int = 40

    # --- Notifications ----------------------------------------------------
    notify_channels: str = "email"
    notify_from_email: str = "jobs@example.com"

    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = False

    #: Resend (https://resend.com) — an HTTPS alternative to SMTP, which many
    #: hosts and CI runners block outright.
    resend_api_key: str = ""
    #: Must be a verified sender. Without your own domain Resend only allows
    #: ``onboarding@resend.dev``, which may only send to your account address —
    #: which is exactly what a personal job alert needs.
    resend_from: str = "onboarding@resend.dev"

    slack_webhook_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- Scheduler --------------------------------------------------------
    scheduler_tick_minutes: int = 5
    scheduler_default_interval_minutes: int = 1440

    # --- Parsed views -----------------------------------------------------
    @property
    def proxies(self) -> list[str]:
        return _split_csv(self.scrape_proxies)

    @property
    def blocked_resource_types(self) -> set[str]:
        return set(_split_csv(self.playwright_block_resources))

    @property
    def enabled_notification_channels(self) -> list[str]:
        return _split_csv(self.notify_channels)

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        level = value.upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if level not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}")
        return level


def _split_csv(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so that importing modules and FastAPI dependencies share one
    instance; tests clear the cache via ``get_settings.cache_clear()``.
    """
    return Settings()
