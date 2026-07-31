"""Domain exception hierarchy.

The distinction that matters operationally is ``retryable``: Celery consults it
to decide between retrying a task and failing it permanently. A 503 is worth
another go; a malformed career URL never will be.
"""

from __future__ import annotations


class PlatformError(Exception):
    """Base class for everything this application raises deliberately."""

    retryable: bool = False

    def __init__(self, message: str, **context: object) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def __str__(self) -> str:
        if not self.context:
            return self.message
        details = " ".join(f"{k}={v!r}" for k, v in self.context.items())
        return f"{self.message} ({details})"


# --- Fetching -------------------------------------------------------------


class FetchError(PlatformError):
    retryable = True


class TransientFetchError(FetchError):
    """5xx, timeouts, connection resets — worth retrying."""

    retryable = True


class PermanentFetchError(FetchError):
    """404, malformed URL, unresolvable host — retrying changes nothing."""

    retryable = False


class BlockedError(FetchError):
    """403 / bot wall / CAPTCHA. Retryable once, after escalating to a render."""

    retryable = True


class RobotsDisallowedError(FetchError):
    retryable = False


class RateLimitedError(FetchError):
    retryable = True


# --- Extraction -----------------------------------------------------------


class ExtractionError(PlatformError):
    retryable = False


class NoJobsFoundError(ExtractionError):
    """Extraction ran but produced nothing usable — a learning trigger."""


class LowConfidenceError(ExtractionError):
    def __init__(self, message: str, confidence: float, **context: object) -> None:
        super().__init__(message, confidence=confidence, **context)
        self.confidence = confidence


# --- LLM ------------------------------------------------------------------


class LLMError(PlatformError):
    retryable = True


class LLMBudgetExceededError(LLMError):
    """Daily ceiling hit. Not retryable — the answer will be the same all day."""

    retryable = False


class LLMResponseError(LLMError):
    """Model returned something that did not parse as the requested schema."""


class LLMNotConfiguredError(LLMError):
    retryable = False


# --- Everything else ------------------------------------------------------


class NotificationError(PlatformError):
    retryable = True


class ConfigurationError(PlatformError):
    retryable = False


class NotFoundError(PlatformError):
    retryable = False


class DuplicateError(PlatformError):
    retryable = False
