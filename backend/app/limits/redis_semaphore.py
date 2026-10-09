"""Cluster-wide concurrency limits for LLM / search calls, backed by Redis - failing OPEN.

Bedrock / Jev / web-search quotas are account-level, so per-process semaphores multiply with every worker
container. This module adds a second, cluster-wide gate on top of each per-process one:

- `GlobalSemaphore` - a lease semaphore. A sorted set holds one member per in-flight call, scored by the
  moment its lease expires; a Lua script drops expired members and admits a new one only while fewer than
  `limit` remain (atomic, and the limit is an argument so it can change at runtime, e.g. under AIMD). A
  call whose process died frees its slot when the lease expires.
- `aimd_*` - the shared AIMD state (limit / success streak / last cut) behind `AdaptiveLimiter`, so a
  throttle seen by ONE worker lowers the allowance of ALL of them.

Redis is an optimisation, never a dependency: with no `REDIS_URL`, when the `redis` package is missing, or
after any Redis error (a short circuit breaker then skips Redis for `redis_retry_after_seconds`), every call
proceeds under the per-process limit alone.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import weakref
from collections.abc import Callable
from typing import Any

from app.config import get_settings

logger = logging.getLogger(__name__)

# KEYS[1] zset of in-flight calls (score = lease expiry, ms). ARGV: limit, member, lease_ms.
_ACQUIRE = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[1]) then
  redis.call('ZADD', KEYS[1], now + tonumber(ARGV[3]), ARGV[2])
  redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[3]) * 2)
  return 1
end
return 0
"""

# KEYS[1] hash {limit, ok, cut}. ARGV: maximum, grow_after.  Returns the (possibly raised) limit.
_AIMD_SUCCESS = """
local limit = tonumber(redis.call('HGET', KEYS[1], 'limit') or ARGV[1])
local ok = redis.call('HINCRBY', KEYS[1], 'ok', 1)
if ok >= tonumber(ARGV[2]) and limit < tonumber(ARGV[1]) then
  limit = limit + 1
  redis.call('HSET', KEYS[1], 'limit', limit, 'ok', 0)
else
  redis.call('HSETNX', KEYS[1], 'limit', limit)
end
redis.call('PEXPIRE', KEYS[1], 3600000)
return limit
"""

# ARGV: maximum, minimum, cooldown_ms.  Halves the limit at most once per cooldown. Returns the limit.
_AIMD_THROTTLE = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local limit = tonumber(redis.call('HGET', KEYS[1], 'limit') or ARGV[1])
local cut = tonumber(redis.call('HGET', KEYS[1], 'cut') or 0)
redis.call('HSET', KEYS[1], 'ok', 0)
if now - cut >= tonumber(ARGV[3]) then
  limit = math.max(tonumber(ARGV[2]), math.floor(limit / 2))
  redis.call('HSET', KEYS[1], 'limit', limit, 'cut', now)
end
redis.call('PEXPIRE', KEYS[1], 3600000)
return limit
"""


class _Gate:
    """Per-event-loop Redis connection + a circuit breaker. All Redis traffic goes through `run()`."""

    def __init__(self) -> None:
        self._clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, tuple[str, Any]] = (
            weakref.WeakKeyDictionary()
        )
        self._down_until = 0.0
        self._warned = False

    def enabled(self) -> bool:
        return bool(get_settings().redis_url) and time.monotonic() >= self._down_until

    def _client(self) -> Any:
        url = get_settings().redis_url
        loop = asyncio.get_running_loop()
        cached = self._clients.get(loop)
        if cached is None or cached[0] != url:
            import redis.asyncio as aioredis  # lazy: a missing package just disables the shared limits

            client = aioredis.from_url(
                url, socket_timeout=1.0, socket_connect_timeout=1.0, health_check_interval=30,
                decode_responses=True,
            )
            self._clients[loop] = cached = (url, client)
        return cached[1]

    def mark_down(self, exc: BaseException) -> None:
        self._down_until = time.monotonic() + get_settings().redis_retry_after_seconds
        if not self._warned:
            logger.warning("Redis unavailable (%s); using per-process limits only.", exc)
            self._warned = True

    def mark_up(self) -> None:
        if self._warned:
            logger.info("Redis reachable again; cluster-wide limits restored.")
            self._warned = False

    async def run(self, op: Callable[[Any], Any]) -> Any:
        """`await op(client)`; any failure trips the breaker and raises `RedisUnavailable`."""
        if not self.enabled():
            raise RedisUnavailable("disabled")
        try:
            result = await op(self._client())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - connection, timeout, script or import errors alike
            self.mark_down(exc)
            raise RedisUnavailable(str(exc)) from exc
        self.mark_up()
        return result

    def reset(self) -> None:
        """Forget the breaker state (tests)."""
        self._down_until = 0.0
        self._warned = False
        self._clients = weakref.WeakKeyDictionary()


class RedisUnavailable(Exception):
    pass


_gate = _Gate()


def gate() -> _Gate:
    return _gate


def redis_enabled() -> bool:
    return _gate.enabled()


async def redis_ping() -> bool | None:
    """True/False = Redis answered / did not; None = not configured."""
    if not get_settings().redis_url:
        return None
    try:
        await _gate.run(lambda r: r.ping())
        return True
    except RedisUnavailable:
        return False


# --- the semaphore ----------------------------------------------------------------------------------


class GlobalSemaphore:
    """`async with sem.slot():` - a per-process cap (`local_limit`) AND a cluster-wide cap (`global_limit`).

    Either limit callable may be None / return <= 0 to mean "no cap at that level". The local part is an
    `asyncio.Semaphore` per event loop (they bind to the loop they are first awaited on; tests run one loop
    per test) sized when first used on that loop. The Redis part is skipped (fail open) whenever Redis is
    off or erroring."""

    def __init__(
        self,
        name: str,
        *,
        local_limit: Callable[[], int] | None,
        global_limit: Callable[[], int] | None,
        lease_seconds: float | None = None,
    ) -> None:
        self.name = name
        self._local_limit = local_limit
        self._global_limit = global_limit
        self._lease_seconds = lease_seconds
        self._locals: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
            weakref.WeakKeyDictionary()
        )

    @property
    def key(self) -> str:
        return f"qs:sem:{self.name}"

    def _local(self) -> asyncio.Semaphore | None:
        if self._local_limit is None:
            return None
        loop = asyncio.get_running_loop()
        sem = self._locals.get(loop)
        if sem is None:
            sem = self._locals[loop] = asyncio.Semaphore(max(1, self._local_limit()))
        return sem

    def slot(self) -> _Slot:
        return _Slot(self)

    async def _try_acquire(self, member: str, limit: int, lease_ms: int) -> bool:
        return bool(await _gate.run(lambda r: r.eval(_ACQUIRE, 1, self.key, limit, member, lease_ms)))

    async def holders(self) -> int | None:
        """Calls currently holding a cluster-wide slot (None when Redis is unavailable). For tests / metrics."""
        try:
            return int(await _gate.run(lambda r: r.zcount(self.key, int(time.time() * 1000), "+inf")))
        except RedisUnavailable:
            return None


class _Slot:
    def __init__(self, sem: GlobalSemaphore) -> None:
        self._sem = sem
        self._local: asyncio.Semaphore | None = None
        self._member: str | None = None

    async def __aenter__(self) -> None:
        sem = self._sem
        self._local = sem._local()
        if self._local is not None:
            await self._local.acquire()
        try:
            await self._acquire_global()
        except BaseException:
            # Cancelled (or failed) while a Lua ADD may already have landed: give that slot back now instead of
            # leaving it to expire (a leaked slot would shrink the cluster allowance for `redis_slot_lease_seconds`).
            await self._release_global_quietly()
            if self._local is not None:
                self._local.release()
            raise

    async def _acquire_global(self) -> None:
        sem = self._sem
        if sem._global_limit is None:
            return
        lease_s = sem._lease_seconds or get_settings().redis_slot_lease_seconds
        member = f"{id(self):x}-{random.getrandbits(48):x}-{time.monotonic_ns():x}"
        self._member = member  # recorded BEFORE the first attempt so a cancel mid-acquire can release it
        while True:
            limit = sem._global_limit()
            if limit <= 0 or not _gate.enabled():
                self._member = None
                return  # no cluster cap, or Redis is down: the per-process cap alone applies
            try:
                if await sem._try_acquire(member, limit, int(lease_s * 1000)):
                    return
            except RedisUnavailable:
                self._member = None
                return  # fail open
            await asyncio.sleep(0.05 + 0.1 * random.random())

    async def _release_global_quietly(self) -> None:
        member, self._member = self._member, None
        if member is None or not _gate.enabled():
            return
        key = self._sem.key
        try:
            await asyncio.shield(_gate.run(lambda r: r.zrem(key, member)))
        except (RedisUnavailable, asyncio.CancelledError):
            pass  # the lease expires on its own

    async def __aexit__(self, *exc_info: object) -> None:
        try:
            if self._member is not None and _gate.enabled():
                member, key = self._member, self._sem.key
                try:
                    await asyncio.shield(_gate.run(lambda r: r.zrem(key, member)))  # survives a cancel
                except RedisUnavailable:
                    pass  # the lease expires on its own
        finally:
            self._member = None
            if self._local is not None:
                self._local.release()


_semaphores: dict[str, GlobalSemaphore] = {}


def get_semaphore(
    name: str,
    *,
    local_limit: Callable[[], int] | None,
    global_limit: Callable[[], int] | None,
) -> GlobalSemaphore:
    """Process-wide registry, so every call site of one resource shares the same semaphore object."""
    sem = _semaphores.get(name)
    if sem is None:
        sem = _semaphores[name] = GlobalSemaphore(name, local_limit=local_limit, global_limit=global_limit)
    return sem


# --- shared AIMD state ------------------------------------------------------------------------------


def _aimd_key(name: str) -> str:
    return f"qs:aimd:{name}"


async def aimd_get(name: str, maximum: int) -> int | None:
    """The cluster's current AIMD limit, or None when Redis is unavailable / holds no state yet."""
    try:
        raw = await _gate.run(lambda r: r.hget(_aimd_key(name), "limit"))
    except RedisUnavailable:
        return None
    return None if raw is None else max(1, min(maximum, int(raw)))


async def aimd_success(name: str, maximum: int, grow_after: int) -> int | None:
    try:
        raw = await _gate.run(lambda r: r.eval(_AIMD_SUCCESS, 1, _aimd_key(name), maximum, grow_after))
    except RedisUnavailable:
        return None
    return int(raw)


async def aimd_throttle(name: str, maximum: int, minimum: int, cooldown_seconds: float) -> int | None:
    try:
        raw = await _gate.run(
            lambda r: r.eval(_AIMD_THROTTLE, 1, _aimd_key(name), maximum, minimum, int(cooldown_seconds * 1000))
        )
    except RedisUnavailable:
        return None
    return int(raw)
