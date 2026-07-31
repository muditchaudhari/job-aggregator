"""LLM layer: response parsing, cost accounting, budget enforcement."""

from __future__ import annotations

from typing import Any, cast

import pytest

from app.core.errors import LLMBudgetExceededError, LLMNotConfiguredError, LLMResponseError
from app.llm.base import estimate_cost, parse_json_response
from app.llm.client import LLMClient, UsageTally
from app.llm.providers import NullProvider
from tests.fixtures.fakes import FakeBudgetTracker, FakeLLMProvider


class TestResponseParsing:
    def test_plain_json(self) -> None:
        assert parse_json_response('{"a": 1}') == {"a": 1}

    def test_fenced_json(self) -> None:
        """Models wrap output in code fences often enough to handle here."""
        assert parse_json_response('```json\n{"a": 1}\n```') == {"a": 1}

    def test_unlabelled_fence(self) -> None:
        assert parse_json_response('```\n{"a": 1}\n```') == {"a": 1}

    def test_json_surrounded_by_prose(self) -> None:
        text = 'Sure! Here are the selectors:\n{"container_selector": "li.job"}\nHope that helps.'
        assert parse_json_response(text) == {"container_selector": "li.job"}

    def test_array_response(self) -> None:
        assert parse_json_response("[1, 2, 3]") == [1, 2, 3]

    def test_unparseable_raises(self) -> None:
        with pytest.raises(LLMResponseError):
            parse_json_response("I could not determine the selectors.")


class TestCostEstimation:
    def test_known_model(self) -> None:
        cost = estimate_cost("claude-sonnet-5", 1_000_000, 100_000)
        assert cost == pytest.approx(3.00 + 1.50)

    def test_prefix_match_handles_dated_ids(self) -> None:
        assert estimate_cost("claude-haiku-4-5-20251001", 1_000_000, 0) == pytest.approx(1.0)

    def test_unknown_model_uses_the_conservative_default(self) -> None:
        """Over-estimating trips the breaker early — the safe direction."""
        assert estimate_cost("some-new-model", 1_000_000, 0) == pytest.approx(3.00)

    def test_longest_prefix_wins(self) -> None:
        """``gemini-3-flash`` must not shadow ``gemini-3-flash-preview``.

        They are priced differently, and a first-match-wins lookup would bill
        the preview at the more expensive general rate.
        """
        preview = estimate_cost("gemini-3-flash-preview", 1_000_000, 0)
        general = estimate_cost("gemini-3-flash", 1_000_000, 0)
        assert preview == pytest.approx(0.25)
        assert general == pytest.approx(0.50)
        assert preview != general


class TestNullProvider:
    def test_is_never_available(self) -> None:
        assert NullProvider().is_available is False

    def test_raises_on_use(self) -> None:
        with pytest.raises(LLMNotConfiguredError):
            NullProvider().complete(prompt="hi")


class TestLLMClient:
    def _client(self, provider: FakeLLMProvider, budget: FakeBudgetTracker) -> LLMClient:
        return LLMClient(provider=provider, budget=cast(Any, budget))

    def test_records_usage_on_the_tally(self) -> None:
        provider = FakeLLMProvider(['{"ok": true}'])
        budget = FakeBudgetTracker()
        tally = UsageTally()

        response = self._client(provider, budget).complete(
            prompt="x" * 400, purpose="selector_generation", tally=tally
        )

        assert response.json() == {"ok": True}
        assert tally.calls == 1
        assert tally.input_tokens == 100
        assert tally.cost_usd > 0
        assert budget.recorded == [response.cost_usd]

    def test_budget_exhaustion_blocks_the_call(self) -> None:
        provider = FakeLLMProvider(['{"ok": true}'])
        budget = FakeBudgetTracker(exhausted=True)

        with pytest.raises(LLMBudgetExceededError):
            self._client(provider, budget).complete(prompt="x", purpose="test")

        assert provider.calls == []

    def test_prompt_is_truncated_to_the_configured_ceiling(self) -> None:
        """A caller that forgot to reduce its input must not blow the budget."""
        from app.core.config import get_settings

        provider = FakeLLMProvider(['{"ok": true}'])
        client = self._client(provider, FakeBudgetTracker())
        limit = get_settings().llm_max_input_chars

        client.complete(prompt="y" * (limit + 5_000), purpose="test")
        assert len(provider.calls[0]["prompt"]) == limit

    def test_provider_failure_counts_against_the_breaker(self) -> None:
        from app.core.errors import LLMError

        provider = FakeLLMProvider(broken=True)
        budget = FakeBudgetTracker()

        with pytest.raises(LLMError):
            self._client(provider, budget).complete(prompt="x", purpose="test")

        assert budget.failures == 1
        assert budget.recorded == []

    def test_unavailable_provider_is_refused_before_the_budget_check(self) -> None:
        client = LLMClient(provider=NullProvider(), budget=cast(Any, FakeBudgetTracker()))
        with pytest.raises(LLMNotConfiguredError):
            client.complete(prompt="x", purpose="test")
