import pytest
import asyncio
from app.rate_limiter import InMemoryRateLimiter
from app.circuit_breaker import CircuitBreaker, CircuitState
from app.cache import InMemorySemanticCache, EmbeddingEngine


@pytest.mark.asyncio
async def test_in_memory_rate_limiter():
    limiter = InMemoryRateLimiter()
    res1 = await limiter.check_rate_limit("t1", requested_tokens=3.0, custom_burst=5.0, custom_refill=1.0)
    assert res1.allowed is True
    assert res1.remaining <= 2

    res2 = await limiter.check_rate_limit("t1", requested_tokens=3.0, custom_burst=5.0, custom_refill=1.0)
    assert res2.allowed is False
    assert res2.retry_after > 0


@pytest.mark.asyncio
async def test_circuit_breaker_transitions():
    cb = CircuitBreaker(
        name="test-cb",
        failure_threshold=2,
        recovery_timeout=0.1,
        half_open_success_threshold=1
    )
    assert cb.state == CircuitState.CLOSED

    await cb.record_failure(Exception("e1"))
    assert cb.state == CircuitState.CLOSED

    await cb.record_failure(Exception("e2"))
    assert cb.state == CircuitState.OPEN

    assert await cb.can_execute() is False

    await asyncio.sleep(0.15)
    assert await cb.can_execute() is True
    assert cb.state == CircuitState.HALF_OPEN

    await cb.record_success()
    assert cb.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_cache_threshold():
    embedder = EmbeddingEngine()
    cache = InMemorySemanticCache(embedding_engine=embedder)

    await cache.set("t1", "Explain neural networks", "Response text")

    hit = await cache.get("t1", "Explain neural networks")
    assert hit is not None
    assert hit[0] == "Response text"
    assert hit[1] >= cache.threshold

    miss = await cache.get("t1", "Completely unrelated topic about cooking pizza")
    assert miss is None
