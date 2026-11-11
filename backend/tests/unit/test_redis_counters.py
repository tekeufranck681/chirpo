from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from chirpo.adapters.ratelimit.redis import RedisCounters
from chirpo.core.errors import CountersUnavailableError


class Pipeline:
    def __init__(self, redis: "FakeRedis") -> None:
        self.redis = redis
        self.queued: list[tuple[str, tuple[Any, ...]]] = []

    async def __aenter__(self) -> "Pipeline":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def incrby(self, key: str, amount: int) -> None:
        self.queued.append(("incrby", (key, amount)))

    def expire(self, key: str, seconds: int) -> None:
        self.queued.append(("expire", (key, seconds)))

    async def execute(self) -> list[Any]:
        if self.redis.down:
            raise RedisConnectionError("refused")
        results: list[Any] = []
        for name, args in self.queued:
            if name == "incrby":
                key, amount = args
                self.redis.values[key] = self.redis.values.get(key, 0) + amount
                results.append(self.redis.values[key])
            else:
                self.redis.ttls[args[0]] = args[1]
                results.append(True)
        return results


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.down = False

    def pipeline(self, transaction: bool = True) -> Pipeline:
        assert transaction  # the increment and its expiry go together
        return Pipeline(self)

    async def get(self, key: str) -> bytes | None:
        if self.down:
            raise RedisConnectionError("refused")
        return str(self.values[key]).encode() if key in self.values else None


async def test_adding_returns_the_total_and_sets_the_expiry() -> None:
    redis = FakeRedis()
    counters = RedisCounters(redis)  # type: ignore[arg-type]
    assert await counters.add("k", 1, 3600) == 1
    assert await counters.add("k", 4, 3600) == 5
    assert redis.ttls["k"] == 3600
    assert await counters.get("k") == 5


async def test_a_missing_counter_is_zero() -> None:
    assert await RedisCounters(FakeRedis()).get("none") == 0  # type: ignore[arg-type]


@pytest.mark.parametrize("error", [OSError("reset"), TimeoutError(), ValueError("corrupt")])
async def test_network_and_data_errors_become_the_cores_error(error: Exception) -> None:
    class Broken(FakeRedis):
        async def get(self, key: str) -> bytes | None:
            raise error

        def pipeline(self, transaction: bool = True) -> Pipeline:
            raise error

    counters = RedisCounters(Broken())  # type: ignore[arg-type]
    with pytest.raises(CountersUnavailableError):
        await counters.add("k", 1, 60)
    with pytest.raises(CountersUnavailableError):
        await counters.get("k")


async def test_a_corrupt_value_is_not_a_crash() -> None:
    redis = FakeRedis()
    redis.values["k"] = "x"  # type: ignore[assignment]
    with pytest.raises(CountersUnavailableError):
        await RedisCounters(redis).get("k")  # type: ignore[arg-type]


async def test_redis_errors_become_the_cores_error() -> None:
    redis = FakeRedis()
    redis.down = True
    counters = RedisCounters(redis)  # type: ignore[arg-type]
    with pytest.raises(CountersUnavailableError):
        await counters.add("k", 1, 60)
    with pytest.raises(CountersUnavailableError):
        await counters.get("k")
