"""Regression tests: Redis clear/reset must target the same keys that get/set/check write.

Uses a minimal in-process fake of the redis.asyncio client (bytes in, bytes out, like
decode_responses=False) so no Redis server is required.
"""
import fnmatch

import pytest

from app.cache import EmbeddingEngine, SemanticCache
from app.rate_limiter import RedisRateLimiter


def _b(value):
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8")


class FakePipeline:
    def __init__(self, redis):
        self._redis = redis
        self._ops = []

    def __getattr__(self, name):
        def queue(*args, **kwargs):
            self._ops.append((name, args, kwargs))
            return self
        return queue

    async def execute(self):
        results = []
        for name, args, kwargs in self._ops:
            results.append(await getattr(self._redis, name)(*args, **kwargs))
        self._ops = []
        return results


class FakeRedis:
    def __init__(self):
        self.sets = {}
        self.hashes = {}

    def pipeline(self, transaction=True):
        return FakePipeline(self)

    def register_script(self, script):
        async def run(keys, args):
            # Emulates the token-bucket Lua script's write to KEYS[1].
            await self.hset(keys[0], mapping={"tokens": args[0] - args[2], "last_updated": args[3]})
            return [1, str(args[0] - args[2]), "0", str(args[0]), "1"]
        return run

    async def sadd(self, key, *members):
        self.sets.setdefault(_b(key), set()).update(_b(m) for m in members)

    async def srem(self, key, *members):
        self.sets.get(_b(key), set()).difference_update(_b(m) for m in members)

    async def smembers(self, key):
        return set(self.sets.get(_b(key), set()))

    async def hset(self, key, mapping):
        self.hashes.setdefault(_b(key), {}).update({_b(k): _b(v) for k, v in mapping.items()})

    async def hgetall(self, key):
        return dict(self.hashes.get(_b(key), {}))

    async def expire(self, key, ttl):
        return True

    async def delete(self, *keys):
        for key in keys:
            self.sets.pop(_b(key), None)
            self.hashes.pop(_b(key), None)

    async def scan_iter(self, match):
        for key in list(self.sets) + list(self.hashes):
            if fnmatch.fnmatchcase(key.decode(), match):
                yield key

    def all_keys(self):
        return {k.decode() for k in list(self.sets) + list(self.hashes)}


@pytest.mark.asyncio
async def test_redis_semantic_cache_clear_removes_written_entries():
    redis = FakeRedis()
    cache = SemanticCache(redis, embedding_engine=EmbeddingEngine())

    await cache.set("t1", "Explain neural networks", "resp-1")
    await cache.set("t2", "Explain neural networks", "resp-2")
    await redis.sadd("unrelated:key", "x")
    assert await cache.get("t1", "Explain neural networks") is not None

    await cache.clear("t1")
    assert await cache.get("t1", "Explain neural networks") is None
    assert not any(k.startswith("semantic_cache:t1:") for k in redis.all_keys())
    assert await cache.get("t2", "Explain neural networks") is not None

    await cache.clear()
    assert await cache.get("t2", "Explain neural networks") is None
    assert redis.all_keys() == {"unrelated:key"}


@pytest.mark.asyncio
async def test_redis_rate_limiter_reset_removes_bucket_keys():
    redis = FakeRedis()
    limiter = RedisRateLimiter(redis)

    await limiter.check_rate_limit("t1", requested_tokens=1.0, custom_burst=5.0, custom_refill=1.0)
    await limiter.check_rate_limit("t2", requested_tokens=1.0, custom_burst=5.0, custom_refill=1.0)
    await redis.sadd("unrelated:key", "x")
    bucket_keys = {k for k in redis.all_keys() if k != "unrelated:key"}
    assert len(bucket_keys) == 2

    await limiter.reset("t1")
    assert len({k for k in redis.all_keys() if k != "unrelated:key"}) == 1

    await limiter.reset()
    assert redis.all_keys() == {"unrelated:key"}
