"""Structured logging.

Human-readable in development, JSON in production. Correlation happens through
``structlog.contextvars`` so a scan's company id is attached to every log line
emitted anywhere beneath it, without threading a logger through call signatures.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog

from app.core.config import get_settings


def configure_logging() -> None:
    settings = get_settings()

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, settings.log_level),
    )
    # These libraries are chatty at INFO and say nothing we act on.
    # google_genai logs "AFC is enabled with max remote calls: 10" at INFO on
    # every single call, which drowns the CLI's own output.
    for noisy in (
        "httpx", "httpcore", "urllib3", "asyncio", "playwright",
        "google_genai", "google_genai.models", "google.genai",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if settings.log_json:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty()))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


@contextmanager
def log_context(**kwargs: Any) -> Iterator[None]:
    """Bind values for the duration of a block, then restore.

    Used at the top of a scan so every downstream log line carries
    ``company_id`` / ``run_id`` without explicit passing.
    """
    tokens = structlog.contextvars.bind_contextvars(**kwargs)
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)
