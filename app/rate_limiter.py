import time
import logging
import asyncio
from dataclasses import dataclass
from typing import Dict, Any, Optional
import redis.asyncio as aioredis
from redis.exceptions import RedisError, ConnectionError, TimeoutError

from app.config import settings

logger = logging.getLogger("neural_gateway.rate_limiter")

# Atomic Token Bucket Lua Script
# Guarantees distributed ACID compliance without race conditions or locks
TOKEN_BUCKET_LUA_SCRIPT = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local requested = tonumber(ARGV[3])
local now = tonumber(ARGV[4])

-- Retrieve current state
local data = redis.call("HMGET", key, "tokens", "last_updated")
local last_tokens = tonumber(data[1])
local last_updated = tonumber(data[2])

if not last_tokens or not last_updated then
    last_tokens = capacity
    last_updated = now
end

-- Calculate time delta and replenished tokens
local elapsed = math.max(0, now - last_updated)
local tokens = math.min(capacity, last_tokens + (elapsed * refill_rate))

local allowed = 0
local remaining = tokens
local retry_after = 0

if tokens >= requested then
    allowed = 1
    tokens = tokens - requested
    remaining = tokens
    redis.call("HSET", key, "tokens", tokens, "last_updated", now)
    
    -- Auto-expire inactive buckets to prevent memory leaks
    local ttl = math.ceil((capacity / refill_rate) * 2)
    if ttl < 60 then ttl = 60 end
    redis.call("EXPIRE", key, ttl)
else
    allowed = 0
    remaining = tokens
    retry_after = (requested - tokens) / refill_rate
    redis.call("HSET", key, "tokens", tokens, "last_updated", now)
    
    local ttl = math.ceil((capacity / refill_rate) * 2)
    if ttl < 60 then ttl = 60 end
    redis.call("EXPIRE", key, ttl)
end

local reset_seconds = math.ceil((capacity - remaining) / refill_rate)
return { allowed, tostring(remaining), tostring(retry_after), tostring(capacity), tostring(reset_seconds) }
"""


@dataclass
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_after: int
    retry_after: float

    @property
    def headers(self) -> Dict[str, str]:
        headers = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
            "X-RateLimit-Reset": str(self.reset_after),
        }
        if not self.allowed and self.retry_after > 0:
            headers["Retry-After"] = str(max(1, int(self.retry_after)))
        return headers


class RedisRateLimiter:
    def __init__(self, redis_client: aioredis.Redis):
        self.redis = redis_client
        self.script = self.redis.register_script(TOKEN_BUCKET_LUA_SCRIPT)

    async def check_rate_limit(
        self,
        tenant_id: str,
        requested_tokens: float = 1.0,
        custom_burst: Optional[float] = None,
        custom_refill: Optional[float] = None,
    ) -> RateLimitResult:
        if not settings.RATE_LIMIT_ENABLED:
            return RateLimitResult(
                allowed=True,
                limit=int(settings.RATE_LIMIT_BURST_CAPACITY),
                remaining=int(settings.RATE_LIMIT_BURST_CAPACITY),
                reset_after=0,
                retry_after=0.0,
            )

        key = f"ratelimit:{tenant_id}"
        capacity = custom_burst or settings.RATE_LIMIT_BURST_CAPACITY
        refill_rate = custom_refill or settings.RATE_LIMIT_TOKENS_PER_SECOND
        now = time.time()

        for attempt in range(1, settings.REDIS_RETRY_ATTEMPTS + 1):
            try:
                res = await self.script(
                    keys=[key],
                    args=[capacity, refill_rate, requested_tokens, now],
                )
                
                allowed_flag = int(res[0]) == 1
                remaining_tokens = max(0, int(float(res[1])))
                retry_after = max(0.0, float(res[2]))
                limit = int(float(res[3]))
                reset_after = max(0, int(float(res[4])))

                return RateLimitResult(
                    allowed=allowed_flag,
                    limit=limit,
                    remaining=remaining_tokens,
                    reset_after=reset_after,
                    retry_after=retry_after,
                )
            except (ConnectionError, TimeoutError, RedisError) as e:
                logger.warning(
                    f"Redis error during rate limiting (attempt {attempt}/{settings.REDIS_RETRY_ATTEMPTS}): {e}"
                )
                if attempt == settings.REDIS_RETRY_ATTEMPTS:
                    logger.error(
                        f"All Redis rate limiter retries exhausted for tenant {tenant_id}. Failing open for resiliency."
                    )
                    # Fail open in production to prevent blocking customers on Redis downtime
                    return RateLimitResult(
                        allowed=True,
                        limit=int(capacity),
                        remaining=int(capacity),
                        reset_after=0,
                        retry_after=0.0,
                    )
                await asyncio.sleep(settings.REDIS_RETRY_DELAY * attempt)

    async def reset(self, tenant_id: Optional[str] = None) -> None:
        """Resets rate limiting bucket in Redis."""
        try:
            if tenant_id:
                await self.redis.delete(f"rate:bucket:{tenant_id}")
            else:
                keys = [k async for k in self.redis.scan_iter("rate:bucket:*")]
                if keys:
                    await self.redis.delete(*keys)
            logger.info("Redis rate limit buckets reset.")
        except Exception as e:
            logger.warning(f"Failed to reset Redis rate limit buckets: {e}")


class InMemoryRateLimiter:
    """Thread-safe in-memory token bucket rate limiter for local / degraded mode."""

    def __init__(self):
        self._buckets: Dict[str, Dict[str, float]] = {}
        self._lock = asyncio.Lock()

    async def check_rate_limit(
        self,
        tenant_id: str,
        requested_tokens: float = 1.0,
        custom_burst: Optional[float] = None,
        custom_refill: Optional[float] = None,
    ) -> RateLimitResult:
        if not settings.RATE_LIMIT_ENABLED:
            return RateLimitResult(
                allowed=True,
                limit=int(settings.RATE_LIMIT_BURST_CAPACITY),
                remaining=int(settings.RATE_LIMIT_BURST_CAPACITY),
                reset_after=0,
                retry_after=0.0,
            )

        capacity = custom_burst or settings.RATE_LIMIT_BURST_CAPACITY
        refill_rate = custom_refill or settings.RATE_LIMIT_TOKENS_PER_SECOND
        now = time.time()

        async with self._lock:
            bucket = self._buckets.get(tenant_id)
            if not bucket:
                bucket = {"tokens": capacity, "last_updated": now}
                self._buckets[tenant_id] = bucket

            elapsed = max(0.0, now - bucket["last_updated"])
            tokens = min(capacity, bucket["tokens"] + (elapsed * refill_rate))

            if tokens >= requested_tokens:
                tokens -= requested_tokens
                bucket["tokens"] = tokens
                bucket["last_updated"] = now
                reset_after = max(0, int((capacity - tokens) / refill_rate))
                return RateLimitResult(
                    allowed=True,
                    limit=int(capacity),
                    remaining=int(tokens),
                    reset_after=reset_after,
                    retry_after=0.0,
                )
            else:
                retry_after = max(0.0, (requested_tokens - tokens) / refill_rate)
                bucket["tokens"] = tokens
                bucket["last_updated"] = now
                reset_after = max(0, int((capacity - tokens) / refill_rate))
                return RateLimitResult(
                    allowed=False,
                    limit=int(capacity),
                    remaining=int(tokens),
                    reset_after=reset_after,
                    retry_after=retry_after,
                )

    async def reset(self, tenant_id: Optional[str] = None) -> None:
        """Resets in-memory rate limiting bucket(s)."""
        async with self._lock:
            if tenant_id:
                self._buckets.pop(tenant_id, None)
            else:
                self._buckets.clear()
            logger.info("In-memory rate limit buckets reset.")


