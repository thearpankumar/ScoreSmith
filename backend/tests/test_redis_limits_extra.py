"""More limiter semantics: slot accounting on errors and cancellation, AIMD floor/ceiling/cooldown, local limiter
(no Redis) invariants, `<= 0` meaning "no cap", registry sharing and the circuit breaker's recovery.

Tests marked with `live_redis` need TEST_REDIS_URL (skipped otherwise); the rest run anywhere."""

from __future__ import annotations

import asyncio
import time

import pytest

from app.config import get_settings
from app.limits import redis_semaphore as rs
from app.pipeline.limiter import AdaptiveLimiter
from tests.test_redis_limits import UNREACHABLE, _fresh_gate, live_redis, unreachable_redis  # noqa: F401

# --- local AdaptiveLimiter (no Redis) --------------------------------------------------------------------------------


async def test_limiter_never_goes_below_its_floor_or_above_its_ceiling(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "redis_url", "")
    lim = AdaptiveLimiter(8, minimum=2, grow_after=1, cooldown=0.0)
    for _ in range(10):
        await lim.on_throttle()
    assert lim.limit == 2  # halved down to the floor, never to 0/1
    for _ in range(50):
        await lim.on_success()
    assert lim.limit == 8  # grows back, capped at the ceiling


async def test_limiter_cooldown_blocks_a_second_cut_and_a_throttle_resets_the_success_streak(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "redis_url", "")
    lim = AdaptiveLimiter(8, grow_after=3, cooldown=60.0)
    await lim.on_throttle()
    await lim.on_throttle()
    assert lim.limit == 4  # one cut inside the cooldown
    await lim.on_success()
    await lim.on_success()
    await lim.on_throttle()  # resets the streak (still no second cut: cooldown)
    await lim.on_success()
    await lim.on_success()
    assert lim.limit == 4  # two successes after the reset is below grow_after=3
    await lim.on_success()
    assert lim.limit == 5


def test_limiter_constructor_clamps_nonsense_arguments() -> None:
    lim = AdaptiveLimiter(0, minimum=99, grow_after=0)
    assert lim.maximum == 1 and lim.minimum == 1 and lim.limit == 1


async def test_limiter_blocks_the_n_plus_first_caller_until_a_slot_frees(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "redis_url", "")
    lim = AdaptiveLimiter(2)
    order: list[str] = []
    gate = asyncio.Event()

    async def worker(name: str) -> None:
        async with lim.slot():
            order.append(f"in:{name}")
            await gate.wait()
        order.append(f"out:{name}")

    tasks = [asyncio.create_task(worker(n)) for n in "abc"]
    await asyncio.sleep(0.1)
    assert lim.in_flight == 2 and sorted(o for o in order if o.startswith("in")) == ["in:a", "in:b"]
    gate.set()
    await asyncio.gather(*tasks)
    assert "in:c" in order and lim.in_flight == 0


async def test_limiter_releases_its_slot_when_the_body_raises(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "redis_url", "")
    lim = AdaptiveLimiter(1)
    with pytest.raises(RuntimeError):
        async with lim.slot():
            raise RuntimeError("boom")
    assert lim.in_flight == 0
    async with asyncio.timeout(1):
        async with lim.slot():  # would deadlock if the failed call leaked its slot
            pass


async def test_cancelled_waiter_does_not_leak_a_slot(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "redis_url", "")
    lim = AdaptiveLimiter(1)
    release = asyncio.Event()

    async def holder() -> None:
        async with lim.slot():
            await release.wait()

    h = asyncio.create_task(holder())
    await asyncio.sleep(0.05)
    waiter = asyncio.create_task(lim.slot().__aenter__())
    await asyncio.sleep(0.05)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    await h
    assert lim.in_flight == 0
    async with asyncio.timeout(1):
        async with lim.slot():
            pass


# --- GlobalSemaphore without Redis -----------------------------------------------------------------------------------


async def test_non_positive_limits_mean_no_cap_at_that_level(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "redis_url", "")
    sem = rs.GlobalSemaphore("t-nocap", local_limit=None, global_limit=lambda: 0)
    peak = {"now": 0, "max": 0}

    async def one() -> None:
        async with sem.slot():
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
            await asyncio.sleep(0.02)
            peak["now"] -= 1

    await asyncio.gather(*(one() for _ in range(10)))
    assert peak["max"] == 10


async def test_local_slot_is_released_when_the_body_raises(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "redis_url", "")
    sem = rs.GlobalSemaphore("t-raise", local_limit=lambda: 1, global_limit=None)
    with pytest.raises(ValueError):
        async with sem.slot():
            raise ValueError
    async with asyncio.timeout(1):
        async with sem.slot():
            pass


def test_registry_returns_one_semaphore_per_name() -> None:
    a = rs.get_semaphore("t-registry", local_limit=lambda: 1, global_limit=None)
    b = rs.get_semaphore("t-registry", local_limit=lambda: 99, global_limit=lambda: 5)
    assert a is b and a.key == "qs:sem:t-registry"


async def test_aimd_helpers_return_none_when_redis_is_unavailable(unreachable_redis) -> None:  # noqa: F811
    assert await rs.aimd_get("x", 8) is None
    rs.gate().reset()
    assert await rs.aimd_success("x", 8, 3) is None
    rs.gate().reset()
    assert await rs.aimd_throttle("x", 8, 1, 1.0) is None
    assert await rs.redis_ping() is False  # configured but down is distinguishable from "not configured"


async def test_breaker_recovers_when_redis_comes_back(live_redis, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(get_settings(), "redis_url", UNREACHABLE)
    rs.gate().reset()
    assert await rs.redis_ping() is False and rs.gate().enabled() is False
    monkeypatch.setattr(get_settings(), "redis_url", live_redis)
    await asyncio.sleep(0.3)  # redis_retry_after_seconds is 0.2 in these tests
    assert await rs.redis_ping() is True and rs.gate().enabled() is True


# --- live Redis -------------------------------------------------------------------------------------------------------


async def test_exact_boundary_cap_of_one_serialises_callers(live_redis) -> None:  # noqa: F811
    sem = rs.GlobalSemaphore("t-one", local_limit=None, global_limit=lambda: 1)
    peak = {"now": 0, "max": 0}

    async def one() -> None:
        async with sem.slot():
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
            await asyncio.sleep(0.03)
            peak["now"] -= 1

    await asyncio.gather(*(one() for _ in range(5)))
    assert peak["max"] == 1 and await sem.holders() == 0


async def test_slot_is_returned_when_the_body_raises(live_redis) -> None:  # noqa: F811
    sem = rs.GlobalSemaphore("t-err", local_limit=None, global_limit=lambda: 1)
    with pytest.raises(RuntimeError):
        async with sem.slot():
            assert await sem.holders() == 1
            raise RuntimeError("scoring failed")
    assert await sem.holders() == 0


async def test_cancelled_while_waiting_for_a_cluster_slot_leaks_nothing(live_redis) -> None:  # noqa: F811
    sem = rs.GlobalSemaphore("t-cancel", local_limit=None, global_limit=lambda: 1)
    release = asyncio.Event()

    async def holder() -> None:
        async with sem.slot():
            await release.wait()

    h = asyncio.create_task(holder())
    await asyncio.sleep(0.1)

    async def waiter() -> None:
        async with sem.slot():
            pytest.fail("should never get the slot")

    w = asyncio.create_task(waiter())
    await asyncio.sleep(0.2)
    w.cancel()
    with pytest.raises(asyncio.CancelledError):
        await w
    assert await sem.holders() == 1  # only the real holder
    release.set()
    await h
    assert await sem.holders() == 0


async def test_slot_expires_after_its_lease_and_frees_capacity_for_others(live_redis) -> None:  # noqa: F811
    sem = rs.GlobalSemaphore("t-expire", local_limit=None, global_limit=lambda: 2, lease_seconds=0.3)
    stuck = [rs._Slot(sem), rs._Slot(sem)]
    for s in stuck:
        await s.__aenter__()  # both slots taken, never released
    assert await sem.holders() == 2
    await asyncio.sleep(0.45)
    assert await sem.holders() == 0  # expired by time alone
    async with asyncio.timeout(1):
        async with sem.slot():
            assert await sem.holders() == 1


async def test_different_resource_names_do_not_share_capacity(live_redis) -> None:  # noqa: F811
    a = rs.GlobalSemaphore("t-res-a", local_limit=None, global_limit=lambda: 1)
    b = rs.GlobalSemaphore("t-res-b", local_limit=None, global_limit=lambda: 1)
    started = time.monotonic()
    async with a.slot():
        async with b.slot():
            pass
    assert time.monotonic() - started < 1


async def test_shared_aimd_floor_ceiling_and_cooldown(live_redis) -> None:  # noqa: F811
    assert await rs.aimd_get("t-aimd", 8) is None  # no state yet
    assert await rs.aimd_throttle("t-aimd", 8, 2, 60.0) == 4
    assert await rs.aimd_throttle("t-aimd", 8, 2, 60.0) == 4  # inside the cooldown: no second halving
    assert await rs.aimd_get("t-aimd", 8) == 4
    assert await rs.aimd_get("t-aimd", 3) == 3  # a smaller local ceiling clamps the shared value
    # floor: with no cooldown the limit can be halved repeatedly but never below the minimum
    for _ in range(5):
        last = await rs.aimd_throttle("t-aimd-floor", 8, 2, 0.0)
    assert last == 2
    # ceiling: growth stops at the maximum
    for _ in range(40):
        grown = await rs.aimd_success("t-aimd-floor", 8, 1)
    assert grown == 8


async def test_two_limiters_see_a_throttle_even_when_only_one_called_the_model(live_redis) -> None:  # noqa: F811
    a = AdaptiveLimiter(8, name="t-share2", cooldown=0.0)
    b = AdaptiveLimiter(8, name="t-share2", cooldown=0.0)
    await a.on_throttle()
    async with b.slot():
        assert b.limit == 4


async def test_http_rate_limit_window_is_shared_through_redis(live_redis, monkeypatch) -> None:  # noqa: F811
    """Two API replicas share one moving window: the cap holds across 'processes' (here: two limiter objects)."""
    import app.ratelimit as rl

    monkeypatch.setattr(rl, "_redis_limiter", None)
    monkeypatch.setattr(rl, "_redis_down_until", 0.0)
    try:
        results = [await rl.within_limit("t-shared-window", "3/minute", "ip-1") for _ in range(5)]
        assert results == [True, True, True, False, False]
        rl._redis_limiter = None  # a second "process" builds its own limiter against the same Redis
        assert await rl.within_limit("t-shared-window", "3/minute", "ip-1") is False
        assert await rl.within_limit("t-shared-window", "3/minute", "ip-2") is True  # per-identifier windows
    finally:
        rl._redis_limiter = None
