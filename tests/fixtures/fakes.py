"""Test doubles.

Deliberately hand-written rather than ``unittest.mock``: these stand in for the
two boundaries that must never be crossed in a unit test — the network and the
model provider — and an explicit fake makes the contract they satisfy legible.
"""

from __future__ import annotations

from typing import Any

from app.core.errors import LLMError, PermanentFetchError
from app.llm.base import LLMProvider, LLMResponse
from app.models.enums import ScrapingStrategy
from app.scrapers.fetcher import FetchResult


class FakeFetcher:
    """Serves canned responses keyed by URL, and records what was asked for."""

    def __init__(
        self,
        pages: dict[str, str] | None = None,
        json_pages: dict[str, Any] | None = None,
    ) -> None:
        self.pages = pages or {}
        self.json_pages = json_pages or {}
        self.requested: list[tuple[str, ScrapingStrategy]] = []

    def fetch(
        self,
        url: str,
        *,
        strategy: ScrapingStrategy = ScrapingStrategy.AUTO,
        headers: dict[str, str] | None = None,
        wait_for_selector: str | None = None,
    ) -> FetchResult:
        self.requested.append((url, strategy))
        if url not in self.pages:
            raise PermanentFetchError("no canned page", url=url)
        return FetchResult(
            url=url,
            final_url=url,
            status_code=200,
            text=self.pages[url],
            content_type="text/html",
            strategy=(
                ScrapingStrategy.PLAYWRIGHT
                if strategy is ScrapingStrategy.PLAYWRIGHT
                else ScrapingStrategy.HTTP
            ),
            fetch_ms=5,
        )

    def fetch_json(self, url: str, *, headers: dict[str, str] | None = None) -> Any:
        self.requested.append((url, ScrapingStrategy.API))
        # Prefix match so paginated endpoints (``?limit=100&offset=0``) resolve
        # against a single registered base URL.
        for key, payload in self.json_pages.items():
            if url.startswith(key):
                return payload
        raise PermanentFetchError("no canned JSON", url=url)

    def post_json(
        self, url: str, payload: Any, *, headers: dict[str, str] | None = None
    ) -> Any:
        self.requested.append((url, ScrapingStrategy.API))
        for key, response in self.json_pages.items():
            if url.startswith(key):
                return response
        raise PermanentFetchError("no canned JSON", url=url)

    def close(self) -> None:
        return None

    def __enter__(self) -> FakeFetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class FakeLLMProvider(LLMProvider):
    """Returns queued responses in order, then repeats the last one."""

    name = "fake"

    def __init__(
        self,
        responses: list[str] | None = None,
        *,
        model: str = "fake-model",
        fail: bool = False,
        broken: bool = False,
    ) -> None:
        """``fail`` means unconfigured; ``broken`` means configured but erroring.

        The two are genuinely different states and the client treats them
        differently — one is refused before the budget check, the other counts
        against the circuit breaker.
        """
        super().__init__(model, api_key="test-key")
        self.responses = responses or ["{}"]
        self.fail = fail
        self.broken = broken
        self.calls: list[dict[str, Any]] = []

    @property
    def is_available(self) -> bool:
        return not self.fail

    def complete(
        self,
        *,
        prompt: str,
        system: str | None = None,
        max_tokens: int = 2000,
        temperature: float = 0.0,
        json_mode: bool = True,
    ) -> LLMResponse:
        self.calls.append({"prompt": prompt, "system": system})
        if self.fail or self.broken:
            raise LLMError("fake provider configured to fail")
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return self._finish(
            text=self.responses[index],
            input_tokens=len(prompt) // 4,
            output_tokens=50,
            latency_ms=1,
        )


class FakeBudgetTracker:
    """Budget tracker with no Redis behind it."""

    def __init__(self, *, exhausted: bool = False) -> None:
        self.exhausted = exhausted
        self.recorded: list[float] = []
        self.failures = 0

    def check(self) -> None:
        if self.exhausted:
            from app.core.errors import LLMBudgetExceededError

            raise LLMBudgetExceededError("budget exhausted (test)")

    def record(self, cost_usd: float) -> None:
        self.recorded.append(cost_usd)

    def record_failure(self) -> None:
        self.failures += 1

    def record_success(self) -> None:
        return None


class RecordingSender:
    """Notification channel that captures instead of sending."""

    def __init__(self, channel: Any, *, fail: bool = False) -> None:
        self.channel = channel
        self.fail = fail
        self.sent: list[tuple[str, Any]] = []

    def is_configured(self) -> bool:
        return True

    def send(self, user: Any, payload: Any) -> None:
        if self.fail:
            from app.core.errors import NotificationError

            raise NotificationError("channel unavailable (test)")
        self.sent.append((user.email, payload))
