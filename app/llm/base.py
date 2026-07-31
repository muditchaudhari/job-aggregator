"""LLM provider interface.

Deliberately narrow: one method, text in and text out, plus token accounting.
Every use in this system — generating selectors, extracting fields, scoring a
match — is "given this context, return this JSON", so a richer abstraction
(tools, streaming, multi-turn) would be surface area that nothing calls and
every new provider would have to implement.

Token and cost accounting are part of the interface rather than an add-on,
because the budget breaker (AD-5) is only as good as the numbers it is fed.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.core.errors import LLMResponseError

#: Approximate USD per 1M tokens, ``(input, output)``. Used for the budget
#: breaker and cost metrics, not for billing. Provider price changes only
#: shift our own throttle point, so a stale entry degrades gracefully.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    # Anthropic
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    # OpenAI
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    # Google. On the free tier the real ceiling is requests per day, not
    # spend, so these figures only drive the local budget breaker — they do
    # not correspond to an invoice unless the key is on a paid plan.
    #
    # Note the prefix matcher below is longest-first, so "gemini-3-flash" does
    # not accidentally shadow "gemini-3-flash-preview" (they differ in price).
    "gemini-3-flash-preview": (0.25, 1.50),
    "gemini-3-flash": (0.50, 3.00),
    "gemini-3.1-flash-lite": (0.10, 0.40),
    "gemini-3-pro-preview": (1.25, 10.00),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-1.5-pro": (1.25, 5.00),
}

#: Applied when a model id is not in the table. Deliberately on the high side:
#: over-estimating trips the breaker early, which is the safe direction to be
#: wrong in.
DEFAULT_PRICING = (3.00, 15.00)


@dataclass(slots=True)
class LLMResponse:
    text: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    def json(self) -> Any:
        """Parse the response as JSON, tolerating the usual decorations.

        Models wrap JSON in prose or fenced code blocks often enough that
        treating that as a hard failure would mean retrying — and paying —
        for something trivially recoverable.
        """
        return parse_json_response(self.text)


def parse_json_response(text: str) -> Any:
    cleaned = text.strip()

    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fenced:
        cleaned = fenced.group(1).strip()

    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        pass

    # Last resort: the outermost balanced object or array in the text.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = cleaned.find(opener)
        end = cleaned.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except (json.JSONDecodeError, ValueError):
                continue

    raise LLMResponseError("model response was not valid JSON", preview=text[:200])


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """USD estimate for one call.

    Longest prefix wins, so a specific variant is never shadowed by the more
    general family name it starts with.
    """
    candidates = [k for k in MODEL_PRICING if model.startswith(k)]
    key = max(candidates, key=len) if candidates else ""
    input_price, output_price = MODEL_PRICING.get(key, DEFAULT_PRICING)
    return round(
        (input_tokens / 1_000_000) * input_price
        + (output_tokens / 1_000_000) * output_price,
        6,
    )


def approximate_tokens(text: str) -> int:
    """Rough token count for providers that do not report usage.

    Four characters per token is the standard English approximation. Only used
    when a provider omits usage data; every provider here reports it, so this
    is a safety net rather than the primary path.
    """
    return max(1, len(text) // 4)


class LLMProvider(ABC):
    """Contract every backend implements."""

    name: str = "base"

    def __init__(self, model: str, api_key: str, *, timeout: float = 60.0) -> None:
        self.model = model
        self._api_key = api_key
        self._timeout = timeout

    @abstractmethod
    def complete(
        self,
        *,
        prompt: str,
        system: str | None = None,
        max_tokens: int = 2000,
        temperature: float = 0.0,
        json_mode: bool = True,
    ) -> LLMResponse:
        """Send one request and return the completion."""

    @property
    def is_available(self) -> bool:
        return bool(self._api_key)

    def list_models(self) -> list[str]:
        """Model ids this key can reach, when the provider can tell us.

        Optional: an empty list means "cannot enumerate", not "none available".
        Exists so the diagnostic script can answer "is LLM_MODEL a real model
        for this key?" without reaching into provider internals.
        """
        return []

    def _finish(
        self,
        *,
        text: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
        raw: dict[str, Any] | None = None,
    ) -> LLMResponse:
        return LLMResponse(
            text=text,
            model=self.model,
            provider=self.name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            cost_usd=estimate_cost(self.model, input_tokens, output_tokens),
            raw=raw or {},
        )
