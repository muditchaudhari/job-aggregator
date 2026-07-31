"""Per-domain rate limiting.

Politeness is a property of the target host, not of a worker process (AD-8), so
the bucket lives in Redis and every worker draws from the same one. Without
this, scaling from 1 to 8 workers silently multiplies the load we put on each
career site by eight.
"""

from __future__ import annotations

import threading
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
        # Fallback bucket, used only when Redis is unreachable. Guarded by a
        # lock because a scan runs several companies on separate threads.
        self._local: dict[str, tuple[float, float]] = {}
        self._local_lock = threading.Lock()
        self._warned_offline = False

    def _key(self, domain: str) -> str:
        return f"ratelimit:domain:{domain}"

    def try_acquire(self, domain: str) -> tuple[bool, float]:
        """Take one token. Returns ``(allowed, seconds_until_available)``.

        Without Redis the same bucket is kept in this process instead of being
        abandoned. Letting every request through would be the wrong failure
        mode where it matters most: a single-process cron runner has no Redis
        by design, and it scrapes from a datacentre address that boards already
        treat with more suspicion than a home one. A per-process bucket is
        exactly right there, and only under-counts when several workers share a
        domain — which is the case that has Redis anyway.
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
            if not self._warned_offline:
                self._warned_offline = True
                logger.warning(
                    "rate_limiter.unavailable",
                    error=str(exc),
                    fallback="per-process token bucket",
                )
            return self._try_acquire_local(domain, rate=rate, capacity=capacity)

    def _try_acquire_local(
        self, domain: str, *, rate: float, capacity: float
    ) -> tuple[bool, float]:
        """In-process mirror of the Lua bucket."""
        now = time.monotonic()
        with self._local_lock:
            tokens, updated = self._local.get(domain, (capacity, now))
            tokens = min(capacity, tokens + (now - updated) * rate)
            if tokens >= 1.0:
                self._local[domain] = (tokens - 1.0, now)
                return True, 0.0
            self._local[domain] = (tokens, now)
            return False, (1.0 - tokens) / rate

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
