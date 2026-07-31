"""Per-domain rate limiting.

Politeness is a property of the target host, not of a worker process (AD-8), so
the bucket lives in Redis and every worker draws from the same one. Without
this, scaling from 1 to 8 workers silently multiplies the load we put on each
career site by eight.
"""

from __future__ import annotations

import time

import redis

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Token bucket, evaluated atomically inside Redis.
#:
#: Doing this in Python would mean read-modify-write across a network round
#: trip, and two workers hitting the same domain at the same moment would both
#: read the same token count and both proceed. The whole point of the shared
#: bucket is lost if the arithmetic is not atomic.
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])       -- tokens per second
local capacity = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])

local bucket = redis.call('HMGET', key, 'tokens', 'timestamp')
local tokens = tonumber(bucket[1])
local timestamp = tonumber(bucket[2])

if tokens == nil then
  tokens = capacity
  timestamp = now
end

local elapsed = math.max(0, now - timestamp)
tokens = math.min(capacity, tokens + elapsed * rate)

local allowed = 0
local wait = 0
if tokens >= requested then
  tokens = tokens - requested
  allowed = 1
else
  wait = (requested - tokens) / rate
end

redis.call('HSET', key, 'tokens', tokens, 'timestamp', now)
-- Expire idle buckets so a one-off domain does not occupy memory forever.
redis.call('EXPIRE', key, math.ceil(capacity / rate) + 60)

return {allowed, tostring(wait)}
"""


class RateLimiter:
    """Shared token bucket keyed by registrable domain."""

    def __init__(
        self,
        client: redis.Redis | None = None,
        *,
        requests_per_minute: int | None = None,
    ) -> None:
        settings = get_settings()
        self._client = client or redis.Redis.from_url(
            settings.redis_url, decode_responses=True
        )
        self._rpm = requests_per_minute or settings.scrape_requests_per_minute_per_domain
        self._script = self._client.register_script(_TOKEN_BUCKET_LUA)

    def _key(self, domain: str) -> str:
        return f"ratelimit:domain:{domain}"

    def try_acquire(self, domain: str) -> tuple[bool, float]:
        """Take one token. Returns ``(allowed, seconds_until_available)``.

        A Redis outage returns "allowed" rather than blocking every scrape: the
        limiter is a courtesy mechanism, and failing closed would turn a cache
        problem into a total outage of the product. The retry/backoff logic in
        the fetcher remains as a second line of defence.
        """
        capacity = max(1, self._rpm)
        rate = self._rpm / 60.0
        try:
            allowed, wait = self._script(
                keys=[self._key(domain)],
                args=[rate, capacity, time.time(), 1],
            )
            return bool(int(allowed)), float(wait)
        except redis.RedisError as exc:
            logger.warning("rate_limiter.unavailable", domain=domain, error=str(exc))
            return True, 0.0

    def acquire(self, domain: str, *, max_wait_seconds: float = 30.0) -> bool:
        """Block until a token is available, or give up.

        Returns False on timeout so the caller can defer the company to the
        next scheduler tick instead of holding a worker slot indefinitely.
        """
        deadline = time.monotonic() + max_wait_seconds
        while True:
            allowed, wait = self.try_acquire(domain)
            if allowed:
                return True
            if time.monotonic() + wait > deadline:
                logger.warning("rate_limiter.timeout", domain=domain, wait=wait)
                return False
            time.sleep(min(wait, 1.0))


_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter()
    return _limiter
