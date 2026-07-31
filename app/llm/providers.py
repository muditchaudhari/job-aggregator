"""Concrete LLM providers.

Each is a thin adapter over a vendor SDK. SDKs are imported lazily inside the
constructor so that installing only the extras you use — ``pip install
'.[anthropic]'`` — does not break the others at import time, and so that the
API process does not pay the import cost of three vendor SDKs it will never
call.
"""

from __future__ import annotations

import random
import re
import time
from typing import Any

from app.core.errors import LLMBudgetExceededError, LLMError, LLMNotConfiguredError
from app.core.logging import get_logger
from app.llm.base import LLMProvider, LLMResponse, approximate_tokens

logger = get_logger(__name__)

#: Attempts per Gemini call before a throttle is treated as a real failure.
_GEMINI_MAX_ATTEMPTS = 4

#: Substrings that identify a throttle rather than a fault. Matched against the
#: exception text rather than an SDK error class so that a vendor refactor
#: degrades to "no retry" instead of an ImportError at call time.
_RATE_LIMIT_MARKERS = ("429", "resource_exhausted", "rate limit", "quota", "too many requests")

#: Google returns its own backoff hint; honouring it beats guessing.
_RETRY_DELAY_RE = re.compile(r"retry[_ -]?delay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)", re.I)


#: A rejected or missing key. Distinguished from a throttle because it is
#: permanent — retrying and tripping the breaker helps nobody.
_BAD_KEY_MARKERS = ("api_key_invalid", "api key not valid", "unauthenticated", "permission_denied")

#: The configured model cannot be called by this account. Google gates older
#: models to pre-existing users ahead of a shutdown while ``models.list()``
#: still reports them, so "listed" and "callable" are genuinely different
#: things and this is a configuration fault, not a transient one.
_MODEL_GONE_MARKERS = (
    "no longer available",
    "is not found for api version",
    "not supported for generatecontent",
)


#: A 429 that names a *daily* quota. Critically different from a per-minute
#: throttle: sleeping 60 seconds and retrying can never clear it, so retrying
#: just burns minutes per call and then repeats for every remaining company.
#: Google's free tier is a few dozen requests per day per model, so this is the
#: 429 a free key actually hits.
_DAILY_QUOTA_MARKERS = (
    "perday", "per day", "requests per day", "daily limit",
    "quota exceeded for metric",
)


def _is_rate_limited(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _RATE_LIMIT_MARKERS)


def _is_daily_quota(exc: Exception) -> bool:
    text = str(exc).lower().replace("_", "")
    if not _is_rate_limited(exc):
        return False
    return any(marker.replace("_", "") in text for marker in _DAILY_QUOTA_MARKERS)


def _is_bad_api_key(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _BAD_KEY_MARKERS)


def _is_model_unavailable(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _MODEL_GONE_MARKERS)


def _hit_output_limit(response: Any) -> bool:
    """Did generation stop because it ran out of output tokens?"""
    for candidate in getattr(response, "candidates", None) or []:
        reason = str(getattr(candidate, "finish_reason", "") or "").upper()
        if "MAX_TOKEN" in reason:
            return True
    return False


def _retry_after(exc: Exception) -> float | None:
    match = _RETRY_DELAY_RE.search(str(exc))
    if not match:
        return None
    try:
        return min(float(match.group(1)), 60.0)
    except ValueError:
        return None


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, model: str, api_key: str, *, timeout: float = 60.0) -> None:
        super().__init__(model, api_key, timeout=timeout)
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            if not self._api_key:
                raise LLMNotConfiguredError("ANTHROPIC_API_KEY is not set")
            try:
                import anthropic
            except ImportError as exc:
                raise LLMNotConfiguredError(
                    "anthropic package not installed; pip install '.[anthropic]'"
                ) from exc
            self._client = anthropic.Anthropic(api_key=self._api_key, timeout=self._timeout)
        return self._client

    def complete(
        self,
        *,
        prompt: str,
        system: str | None = None,
        max_tokens: int = 2000,
        temperature: float = 0.0,
        json_mode: bool = True,
    ) -> LLMResponse:
        client = self._get_client()
        started = time.monotonic()
        try:
            message = client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system or "",
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:
            raise LLMError("Anthropic request failed", detail=str(exc)) from exc

        text = "".join(
            block.text for block in message.content if getattr(block, "type", "") == "text"
        )
        return self._finish(
            text=text,
            input_tokens=getattr(message.usage, "input_tokens", 0),
            output_tokens=getattr(message.usage, "output_tokens", 0),
            latency_ms=int((time.monotonic() - started) * 1000),
            raw={"stop_reason": getattr(message, "stop_reason", None)},
        )


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, model: str, api_key: str, *, timeout: float = 60.0) -> None:
        super().__init__(model, api_key, timeout=timeout)
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            if not self._api_key:
                raise LLMNotConfiguredError("OPENAI_API_KEY is not set")
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise LLMNotConfiguredError(
                    "openai package not installed; pip install '.[openai]'"
                ) from exc
            self._client = OpenAI(api_key=self._api_key, timeout=self._timeout)
        return self._client

    def complete(
        self,
        *,
        prompt: str,
        system: str | None = None,
        max_tokens: int = 2000,
        temperature: float = 0.0,
        json_mode: bool = True,
    ) -> LLMResponse:
        client = self._get_client()
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        started = time.monotonic()
        try:
            completion = client.chat.completions.create(**kwargs)
        except Exception as exc:
            raise LLMError("OpenAI request failed", detail=str(exc)) from exc

        usage = getattr(completion, "usage", None)
        return self._finish(
            text=completion.choices[0].message.content or "",
            input_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
            latency_ms=int((time.monotonic() - started) * 1000),
            raw={"finish_reason": completion.choices[0].finish_reason},
        )


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, model: str, api_key: str, *, timeout: float = 60.0) -> None:
        super().__init__(model, api_key, timeout=timeout)
        self._client: Any = None
        #: Overridable so a diagnostic can probe models without sitting through
        #: a minute of throttle backoff per candidate.
        self.max_attempts = _GEMINI_MAX_ATTEMPTS

    def _get_client(self) -> Any:
        if self._client is None:
            if not self._api_key:
                raise LLMNotConfiguredError("GEMINI_API_KEY is not set")
            try:
                from google import genai
            except ImportError as exc:
                raise LLMNotConfiguredError(
                    "google-genai package not installed; pip install '.[gemini]'"
                ) from exc
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    def list_models(self) -> list[str]:
        try:
            client = self._get_client()
            names: list[str] = []
            for model in client.models.list():
                actions = getattr(model, "supported_actions", None) or []
                # An empty action list means the SDK did not report them;
                # assume usable rather than hiding a model that works.
                if actions and "generateContent" not in actions:
                    continue
                name = str(getattr(model, "name", "")).removeprefix("models/")
                if name:
                    names.append(name)
            return sorted(set(names))
        except Exception as exc:
            logger.warning("gemini.list_models_failed", error=str(exc))
            return []

    def complete(
        self,
        *,
        prompt: str,
        system: str | None = None,
        max_tokens: int = 2000,
        temperature: float = 0.0,
        json_mode: bool = True,
    ) -> LLMResponse:
        client = self._get_client()
        config: dict[str, Any] = {
            "max_output_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            config["system_instruction"] = system
        if json_mode:
            config["response_mime_type"] = "application/json"

        started = time.monotonic()
        response = self._generate_with_retry(client, prompt, config)

        text = getattr(response, "text", "") or ""
        usage = getattr(response, "usage_metadata", None)

        # A truncated response is not a malformed one, and the difference is
        # actionable. Without this the caller sees "not valid JSON" and goes
        # looking for a prompt bug, when the fix is simply more output budget —
        # easy to hit with reasoning models, which spend it before answering.
        if _hit_output_limit(response):
            raise LLMError(
                "Gemini stopped at the output-token limit before completing its "
                f"response; raise LLM_MAX_OUTPUT_TOKENS (currently {max_tokens})",
                model=self.model,
            )
        return self._finish(
            text=text,
            input_tokens=getattr(usage, "prompt_token_count", 0)
            if usage
            else approximate_tokens(prompt),
            output_tokens=getattr(usage, "candidates_token_count", 0)
            if usage
            else approximate_tokens(text),
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    def _generate_with_retry(self, client: Any, prompt: str, config: dict[str, Any]) -> Any:
        """Absorb free-tier throttling before it reaches the circuit breaker.

        On a free key, 429 / RESOURCE_EXHAUSTED is the *expected* response
        under load, not a fault. Letting it surface as an ``LLMError`` would
        count toward the breaker threshold and disable tier 5 for five minutes
        over what is really a "wait a moment" — so it is retried here, and only
        a persistent throttle is reported upward.
        """
        last_error: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                return client.models.generate_content(
                    model=self.model, contents=prompt, config=config
                )
            except Exception as exc:
                last_error = exc
                if _is_daily_quota(exc):
                    # Out for the day. Fail now rather than sleeping through
                    # three backoffs that cannot possibly help.
                    raise LLMBudgetExceededError(
                        "Gemini free-tier daily quota is exhausted for "
                        f"{self.model!r}. It resets on Google's schedule (midnight "
                        "Pacific); until then extraction runs on tiers 1-4 only.",
                    ) from exc
                if not _is_rate_limited(exc) or attempt == self.max_attempts:
                    break
                delay = _retry_after(exc) or min(2.0 * (2 ** (attempt - 1)), 30.0)
                # Jittered, because every worker throttled by the same quota
                # would otherwise retry in lockstep.
                delay += random.uniform(0, delay * 0.25)
                logger.warning(
                    "gemini.rate_limited",
                    attempt=attempt,
                    sleeping=round(delay, 1),
                    model=self.model,
                )
                time.sleep(delay)

        assert last_error is not None
        if _is_bad_api_key(last_error):
            # Not an LLMError: a rejected key is a configuration fault, not a
            # transient one. Raising it as retryable would burn the circuit
            # breaker's budget on an outcome that cannot change, and the raw
            # Google payload is a wall of JSON that buries the actual problem.
            raise LLMNotConfiguredError(
                "Gemini rejected the API key. Check GEMINI_API_KEY in .env — "
                "get a free key at https://aistudio.google.com/apikey"
            ) from last_error
        if _is_model_unavailable(last_error):
            raise LLMNotConfiguredError(
                f"Gemini will not serve {self.model!r} to this account. Set LLM_MODEL "
                "in .env to a callable model and re-run `make check-llm`"
            ) from last_error
        if _is_rate_limited(last_error):
            raise LLMError(
                "Gemini quota exhausted after retries; free-tier daily limit may be spent",
                detail=str(last_error),
            ) from last_error
        raise LLMError("Gemini request failed", detail=str(last_error)) from last_error


class NullProvider(LLMProvider):
    """Explicitly disabled LLM.

    Selected with ``LLM_PROVIDER=null``. Every call raises, which disables
    ladder tier 5 and the semantic matcher while leaving tiers 1-4 and the rule
    matcher fully operational. Useful for cost-free environments and for
    proving that a site is being scraped deterministically.
    """

    name = "null"

    def __init__(self, model: str = "none", api_key: str = "", **_: Any) -> None:
        super().__init__(model, api_key)

    @property
    def is_available(self) -> bool:
        return False

    def complete(self, **_: Any) -> LLMResponse:
        raise LLMNotConfiguredError("LLM support is disabled (LLM_PROVIDER=null)")
