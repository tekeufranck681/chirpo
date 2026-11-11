"""Rate limit counters in Redis, which the worker already uses for its queue (D49)."""

from redis.asyncio import Redis
from redis.exceptions import RedisError

from chirpo.core.errors import CountersUnavailableError

_FAILURES = (RedisError, OSError, TimeoutError, ValueError)  # a down store, or a corrupt value


class RedisCounters:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def add(self, key: str, amount: int, ttl: int) -> int:
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incrby(key, amount)
                pipe.expire(key, ttl)
                total, _ = await pipe.execute()
            return int(total)
        except _FAILURES as error:
            raise CountersUnavailableError(type(error).__name__) from None

    async def get(self, key: str) -> int:
        try:
            value = await self._redis.get(key)
            return int(value) if value else 0
        except _FAILURES as error:
            raise CountersUnavailableError(type(error).__name__) from None
