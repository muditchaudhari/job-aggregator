"""LLM client: the only supported way to call a model.

Providers are deliberately dumb — they translate one request into one vendor
SDK call. Everything that must happen on *every* call regardless of vendor
lives here: budget enforcement, circuit-breaker bookkeeping, metrics, cost
accounting, and structured logging.

Callers never construct a provider directly. If they did, the budget ceiling
would be advisory rather than enforced.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.config import get_settings
from app.core.errors import LLMBudgetExceededError, LLMError, LLMNotConfiguredError
from app.core.logging import get_logger
from app.core.metrics import llm_calls, llm_cost_usd, llm_tokens
from app.llm.base import LLMProvider, LLMResponse
from app.llm.budget import BudgetTracker, get_budget_tracker
from app.llm.providers import (
    AnthropicProvider,
    GeminiProvider,
    NullProvider,
    OpenAIProvider,
)

logger = get_logger(__name__)

_PROVIDERS: dict[str, type[LLMProvider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
    "null": NullProvider,
}


@dataclass(slots=True)
class UsageTally:
    """Per-scan usage, folded into the ``scrape_runs`` row.

    Prometheus counters are fleet-wide; this is what lets a single company's
    run report "this scan cost 1.2 cents", which is the number you need when
    deciding whether a particular site is worth keeping registered.
    """

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)

    def add(self, response: LLMResponse) -> None:
        self.calls += 1
        self.input_tokens += response.input_tokens
        self.output_tokens += response.output_tokens
        self.cost_usd = round(self.cost_usd + response.cost_usd, 6)


def build_provider(
    provider_name: str | None = None, model: str | None = None
) -> LLMProvider:
    settings = get_settings()
    name = (provider_name or settings.llm_provider).lower()

    if not settings.llm_enabled:
        return NullProvider()

    provider_class = _PROVIDERS.get(name)
    if provider_class is None:
        raise LLMNotConfiguredError(f"unknown LLM provider {name!r}")

    api_key = {
        "anthropic": settings.anthropic_api_key,
        "openai": settings.openai_api_key,
        "gemini": settings.gemini_api_key,
        "null": "",
    }[name]

    return provider_class(
        model=model or settings.llm_model,
        api_key=api_key,
        timeout=settings.llm_timeout_seconds,
    )


class LLMClient:
    """Budget-aware wrapper around a provider."""

    def __init__(
        self,
        provider: LLMProvider | None = None,
        budget: BudgetTracker | None = None,
    ) -> None:
        self._settings = get_settings()
        self.provider = provider or build_provider()
        self._budget = budget or get_budget_tracker()

    @property
    def is_available(self) -> bool:
        return self.provider.is_available

    def complete(
        self,
        *,
        prompt: str,
        system: str | None = None,
        purpose: str = "generic",
        max_tokens: int | None = None,
        temperature: float | None = None,
        tally: UsageTally | None = None,
    ) -> LLMResponse:
        """Run one completion under the budget.

        ``purpose`` is a low-cardinality label (``selector_generation``,
        ``field_extraction``, ``job_matching``) that makes the metrics answer
        "what are we actually spending on" without per-company cardinality.
        """
        if not self.is_available:
            raise LLMNotConfiguredError(
                f"LLM provider {self.provider.name!r} is not configured"
            )

        # Truncating here rather than at each call site guarantees the cap
        # holds even for a caller that forgot to reduce its input.
        if len(prompt) > self._settings.llm_max_input_chars:
            logger.warning(
                "llm.prompt_truncated",
                purpose=purpose,
                original=len(prompt),
                limit=self._settings.llm_max_input_chars,
            )
            prompt = prompt[: self._settings.llm_max_input_chars]

        self._budget.check()

        try:
            response = self.provider.complete(
                prompt=prompt,
                system=system,
                max_tokens=max_tokens or self._settings.llm_max_output_tokens,
                temperature=(
                    temperature
                    if temperature is not None
                    else self._settings.llm_temperature
                ),
            )
        except LLMBudgetExceededError:
            # The provider says we are out of quota for the day. Trip the
            # breaker so the remaining companies in this run skip tier 5
            # immediately instead of each discovering it the expensive way.
            self._budget.open_breaker()
            llm_calls.labels(self.provider.name, purpose, "quota").inc()
            logger.error("llm.quota_exhausted", purpose=purpose)
            raise
        except LLMError as exc:
            self._budget.record_failure()
            llm_calls.labels(self.provider.name, purpose, "error").inc()
            if tally is not None:
                tally.errors.append(str(exc))
            logger.error("llm.call_failed", purpose=purpose, error=str(exc))
            raise

        self._budget.record_success()
        self._budget.record(response.cost_usd)

        llm_calls.labels(self.provider.name, purpose, "success").inc()
        llm_tokens.labels(self.provider.name, "input").inc(response.input_tokens)
        llm_tokens.labels(self.provider.name, "output").inc(response.output_tokens)
        llm_cost_usd.labels(self.provider.name).inc(response.cost_usd)

        if tally is not None:
            tally.add(response)

        logger.info(
            "llm.call",
            purpose=purpose,
            model=response.model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_usd=response.cost_usd,
            latency_ms=response.latency_ms,
        )
        return response


_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


def reset_llm_client() -> None:
    """Test-support: drop the cached client so settings changes take effect."""
    global _client
    _client = None
