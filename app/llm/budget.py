"""LLM spend control.

The self-learning path is triggered *by failure* (AD-5), which is exactly the
condition that correlates across companies. One CDN returning a 503 HTML page
to every request makes every extraction fail in the same hour, and without a
ceiling that becomes a thousand selector-regeneration calls.

Two mechanisms:

* a **daily budget**, tracked in Redis so all workers share one counter;
* a **circuit breaker** that opens after repeated provider errors, so an
  outage costs one round of timeouts rather than one per company.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass

import redis

from app.core.config import get_settings
from app.core.errors import LLMBudgetExceededError
from app.core.logging import get_logger
from app.utils.time import utcnow

logger = get_logger(__name__)

_BREAKER_THRESHOLD = 5
_BREAKER_COOLDOWN_SECONDS = 300


@dataclass(slots=True)
class BudgetStatus:
    spent_usd: float
    limit_usd: float
    calls_today: int
    breaker_open: bool

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.limit_usd - self.spent_usd)

    @property
    def exhausted(self) -> bool:
        return self.spent_usd >= self.limit_usd


class BudgetTracker:
    def __init__(self, client: redis.Redis | None = None) -> None:
        settings = get_settings()
        self._settings = settings
        self._client = client or redis.Redis.from_url(
            settings.redis_url, decode_responses=True
        )
        # Used only when Redis is unreachable; see ``status``.
        self._local_spend = 0.0
        self._local_calls = 0
        self._local_breaker_open = False
        self._warned_offline = False

    # -- Keys --------------------------------------------------------------

    @staticmethod
    def _day_key() -> str:
        return utcnow().strftime("%Y%m%d")

    def _spend_key(self) -> str:
        return f"llm:spend:{self._day_key()}"

    def _calls_key(self) -> str:
        return f"llm:calls:{self._day_key()}"

    @staticmethod
    def _breaker_key() -> str:
        return "llm:breaker"

    # -- Budget ------------------------------------------------------------

    def status(self) -> BudgetStatus:
        try:
            spent = float(self._client.get(self._spend_key()) or 0.0)
            calls = int(self._client.get(self._calls_key()) or 0)
            breaker = self._breaker_state()
        except redis.RedisError as exc:
            # No Redis. Fall back to a per-process counter rather than failing
            # closed, because "no Redis" is a legitimate deployment — a cron
            # runner has nothing to share state with, and failing closed there
            # would silently disable the self-learning path for good.
            #
            # A process-local budget still bounds a single run, which is the
            # unit of work in that deployment. Where several workers *do* share
            # a Redis, they share the real budget; this is strictly the
            # degraded case.
            if not self._warned_offline:
                self._warned_offline = True
                logger.warning(
                    "budget.redis_unavailable",
                    error=str(exc),
                    fallback="per-process budget for this run only",
                )
            return BudgetStatus(
                spent_usd=round(self._local_spend, 6),
                limit_usd=self._settings.llm_daily_budget_usd,
                calls_today=self._local_calls,
                breaker_open=self._local_breaker_open,
            )
        return BudgetStatus(
            spent_usd=round(spent, 6),
            limit_usd=self._settings.llm_daily_budget_usd,
            calls_today=calls,
            breaker_open=breaker,
        )

    def check(self) -> None:
        """Raise if the next call must not be made."""
        if not self._settings.llm_budget_breaker_enabled:
            return
        state = self.status()
        if state.breaker_open:
            raise LLMBudgetExceededError(
                "LLM circuit breaker is open after repeated provider failures"
            )
        if state.exhausted:
            raise LLMBudgetExceededError(
                "daily LLM budget exhausted",
                spent=state.spent_usd,
                limit=state.limit_usd,
            )

    def record(self, cost_usd: float) -> None:
        try:
            pipe = self._client.pipeline()
            pipe.incrbyfloat(self._spend_key(), cost_usd)
            pipe.incr(self._calls_key())
            # Two days, not one: a call started at 23:59 must not have its
            # counter vanish while a same-day retry is still in flight.
            pipe.expire(self._spend_key(), 172_800)
            pipe.expire(self._calls_key(), 172_800)
            pipe.execute()
        except redis.RedisError:
            self._local_spend += cost_usd
            self._local_calls += 1

    # -- Circuit breaker ---------------------------------------------------

    def _breaker_state(self) -> bool:
        opened_at = self._client.get(f"{self._breaker_key()}:opened_at")
        if not opened_at:
            return False
        if time.time() - float(opened_at) > _BREAKER_COOLDOWN_SECONDS:
            self.reset_breaker()
            return False
        return True

    def record_failure(self) -> None:
        try:
            failures = int(self._client.incr(f"{self._breaker_key()}:failures"))
            self._client.expire(f"{self._breaker_key()}:failures", _BREAKER_COOLDOWN_SECONDS)
            if failures >= _BREAKER_THRESHOLD:
                self._client.set(f"{self._breaker_key()}:opened_at", time.time())
                logger.error("budget.breaker_opened", failures=failures)
        except redis.RedisError as exc:
            logger.error("budget.breaker_update_failed", error=str(exc))

    def record_success(self) -> None:
        with contextlib.suppress(redis.RedisError):
            self._client.delete(f"{self._breaker_key()}:failures")

    def open_breaker(self) -> None:
        """Force the breaker open — used when the provider reports a hard
        quota stop, which no amount of retrying will clear."""
        self._local_breaker_open = True
        try:
            self._client.set(f"{self._breaker_key()}:opened_at", time.time())
            logger.error("budget.breaker_opened", reason="provider quota exhausted")
        except redis.RedisError as exc:
            logger.error("budget.breaker_open_failed", error=str(exc))

    def reset_breaker(self) -> None:
        with contextlib.suppress(redis.RedisError):
            self._client.delete(
                f"{self._breaker_key()}:opened_at", f"{self._breaker_key()}:failures"
            )


_tracker: BudgetTracker | None = None


def get_budget_tracker() -> BudgetTracker:
    global _tracker
    if _tracker is None:
        _tracker = BudgetTracker()
    return _tracker
