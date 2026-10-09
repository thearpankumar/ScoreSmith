"""Cluster-wide LLM limits (app/limits/redis_semaphore.py).

The fail-open tests need no Redis. The `live_redis` tests run against a real Redis named by
`TEST_REDIS_URL` (CI provides a service container; locally: `docker run -p 6379:6379 redis:7-alpine`) and are
skipped without it. "Two processes" there are independent `GlobalSemaphore` / `AdaptiveLimiter` objects with no
shared Python state - their only link is Redis.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from app.config import get_settings
from app.limits import redis_semaphore as rs
from app.pipeline.limiter import AdaptiveLimiter

UNREACHABLE = "redis://127.0.0.1:1/0"


@pytest.fixture(autouse=True)
def _fresh_gate(monkeypatch):
    rs.gate().reset()
    monkeypatch.setattr(get_settings(), "redis_retry_after_seconds", 0.2)
    yield
    rs.gate().reset()


@pytest.fixture()
def unreachable_redis(monkeypatch):
    monkeypatch.setattr(get_settings(), "redis_url", UNREACHABLE)


@pytest.fixture()
async def live_redis(monkeypatch):
    url = os.environ.get("TEST_REDIS_URL")
    if not url:
        pytest.skip("TEST_REDIS_URL not set")
    monkeypatch.setattr(get_settings(), "redis_url", url)
    try:
        await rs.gate().run(lambda r: r.flushdb())
    except rs.RedisUnavailable:
        pytest.skip("TEST_REDIS_URL is not reachable")
    yield url
    monkeypatch.setattr(get_settings(), "redis_url", url)  # a test may have pointed it elsewhere
    rs.gate().reset()
    await rs.gate().run(lambda r: r.flushdb())


async def _hold_many(sem: rs.GlobalSemaphore, n: int, hold: float, counter: dict) -> None:
    async def one() -> None:
        async with sem.slot():
            counter["now"] += 1
            counter["max"] = max(counter["max"], counter["now"])
            await asyncio.sleep(hold)
            counter["now"] -= 1

    await asyncio.gather(*(one() for _ in range(n)))


# --- fail open (no Redis needed) ---------------------------------------------------------------------------


async def test_semaphore_fails_open_to_the_local_limit_when_redis_is_down(unreachable_redis) -> None:
    sem = rs.GlobalSemaphore("t-down", local_limit=lambda: 3, global_limit=lambda: 1)
    counter = {"now": 0, "max": 0}
    started = time.monotonic()
    await _hold_many(sem, 6, 0.05, counter)
    assert time.monotonic() - started < 5  # never blocks waiting for Redis
    # The cluster cap (1) could not be enforced, but the per-process cap (3) still was.
    assert counter["max"] == 3


async def test_semaphore_without_redis_url_is_purely_local(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "redis_url", "")
    sem = rs.GlobalSemaphore("t-off", local_limit=lambda: 2, global_limit=lambda: 1)
    counter = {"now": 0, "max": 0}
    await _hold_many(sem, 5, 0.02, counter)
    assert counter["max"] == 2
    assert await rs.redis_ping() is None and await sem.holders() is None


async def test_breaker_skips_redis_after_an_error_and_probes_again_later(unreachable_redis, monkeypatch) -> None:
    sem = rs.GlobalSemaphore("t-breaker", local_limit=None, global_limit=lambda: 1)
    async with sem.slot():
        pass
    assert rs.gate().enabled() is False  # tripped by the failed attempt
    await asyncio.sleep(0.3)
    assert rs.gate().enabled() is True  # probes Redis again after redis_retry_after_seconds


async def test_adaptive_limiter_keeps_its_local_aimd_when_redis_is_down(unreachable_redis) -> None:
    limiter = AdaptiveLimiter(8, name="t-aimd-down", cooldown=0.0)
    async with limiter.slot():
        pass
    await limiter.on_throttle()
    assert limiter.limit == 4  # local halving still works
    for _ in range(6):
        await limiter.on_success()
    assert limiter.limit == 5  # ... and local growth


async def test_semaphore_survives_redis_dying_while_a_slot_is_held(live_redis, monkeypatch) -> None:
    sem = rs.GlobalSemaphore("t-mid-flight", local_limit=None, global_limit=lambda: 1)
    async with sem.slot():
        assert await sem.holders() == 1
        monkeypatch.setattr(get_settings(), "redis_url", UNREACHABLE)  # Redis "goes away" mid-call
        rs.gate().reset()
    # the release failed open (swallowed); work continues and later calls are not blocked
    async with sem.slot():
        pass


# --- live Redis --------------------------------------------------------------------------------------------


async def test_cluster_cap_holds_across_independent_semaphores(live_redis) -> None:
    cap = 3
    # Three "processes": same resource name, no shared Python state, each wanting 6 concurrent calls.
    procs = [rs.GlobalSemaphore("t-cap", local_limit=None, global_limit=lambda: cap) for _ in range(3)]
    counter = {"now": 0, "max": 0}
    await asyncio.gather(*(_hold_many(p, 6, 0.1, counter) for p in procs))
    assert counter["max"] <= cap
    assert counter["max"] == cap  # and the cap is actually used, not just never exceeded
    assert await procs[0].holders() == 0  # everything was released


async def test_limit_can_change_at_runtime(live_redis) -> None:
    limit = {"v": 1}
    sem = rs.GlobalSemaphore("t-dynamic", local_limit=None, global_limit=lambda: limit["v"])
    counter = {"now": 0, "max": 0}
    await _hold_many(sem, 4, 0.05, counter)
    assert counter["max"] == 1
    limit["v"] = 4
    counter.update(now=0, max=0)
    await _hold_many(sem, 4, 0.1, counter)
    assert counter["max"] == 4


async def test_leaked_slot_of_a_crashed_process_expires(live_redis) -> None:
    sem = rs.GlobalSemaphore("t-lease", local_limit=None, global_limit=lambda: 1, lease_seconds=0.4)
    leaked = rs._Slot(sem)
    await leaked.__aenter__()  # never released: the owning process "crashed"
    started = time.monotonic()
    async with sem.slot():
        waited = time.monotonic() - started
    assert 0.2 < waited < 3  # got the slot only once the dead lease ran out


async def test_aimd_state_is_shared_between_workers(live_redis) -> None:
    a = AdaptiveLimiter(8, name="t-shared", cooldown=0.5, grow_after=3)
    b = AdaptiveLimiter(8, name="t-shared", cooldown=0.5, grow_after=3)
    await a.on_throttle()  # worker A is throttled by Bedrock ...
    assert a.limit == 4
    async with b.slot():  # ... and worker B (which saw nothing) slows down too
        assert b.limit == 4
    await b.on_throttle()  # a second throttle inside the cooldown does not halve again
    assert b.limit == 4
    for _ in range(3):  # successes seen by either worker raise the shared limit
        await b.on_success()
    assert b.limit == 5
    async with a.slot():
        assert a.limit == 5


async def test_master_limiter_caps_in_flight_calls_across_workers(live_redis) -> None:
    workers = [AdaptiveLimiter(4, name="t-master") for _ in range(3)]
    counter = {"now": 0, "max": 0}

    async def call(limiter: AdaptiveLimiter) -> None:
        async with limiter.slot():
            counter["now"] += 1
            counter["max"] = max(counter["max"], counter["now"])
            await asyncio.sleep(0.05)
            counter["now"] -= 1

    await asyncio.gather(*(call(w) for w in workers for _ in range(8)))
    assert counter["max"] <= 4
