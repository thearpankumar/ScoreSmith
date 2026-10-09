"""Request rate limiting, as FastAPI dependencies.

Built on the `limits` library (the engine under slowapi) rather than slowapi's route decorator: the decorator
wraps the endpoint in a function whose globals are slowapi's, which breaks FastAPI's resolution of this
project's `from __future__ import annotations` type hints. A dependency has no such problem and can also key
on the authenticated user.

Storage: Redis (`REDIS_URL`, shared by every API replica) with a moving window; when Redis is not configured, or
errors, an in-process store is used instead - rate limiting never blocks a request because Redis is down.

    Depends(rate_limit("login", lambda s: s.rate_limit_login))                  # per client IP
    Depends(rate_limit("chat", lambda s: s.rate_limit_chat, per_user=True))     # per signed-in user
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from collections.abc import Callable

from fastapi import HTTPException, Request, status
from limits import parse
from limits.aio.storage import RedisStorage
from limits.aio.strategies import MovingWindowRateLimiter

from app.auth.security import client_ip, peek_user_id
from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


class _MemoryWindow:
    """Tiny in-process sliding window (loop-agnostic, unlike the library's async memory store)."""

    def __init__(self) -> None:
        self._hits: dict[tuple, deque[float]] = defaultdict(deque)

    def hit(self, item, *identifiers: str) -> tuple[bool, int]:
        key = (str(item), *identifiers)
        now = time.time()
        window = float(item.get_expiry())
        q = self._hits[key]
        while q and q[0] <= now - window:
            q.popleft()
        if len(q) >= item.amount:
            return False, max(1, int(q[0] + window - now) + 1)
        q.append(now)
        return True, 0

    def reset(self) -> None:
        self._hits.clear()


_memory = _MemoryWindow()
_redis_limiter: MovingWindowRateLimiter | None = None
_redis_url_in_use: str | None = None
_redis_down_until = 0.0


def _redis():
    """The Redis-backed limiter, or None when Redis is disabled / recently failed."""
    global _redis_limiter, _redis_url_in_use
    s = get_settings()
    if not s.redis_url or time.monotonic() < _redis_down_until:
        return None
    if _redis_limiter is None or _redis_url_in_use != s.redis_url:
        uri = s.redis_url.replace("redis://", "async+redis://", 1).replace("rediss://", "async+rediss://", 1)
        try:
            # `limits` defaults to the `coredis` driver, which this project does not install: use redis-py.
            storage = RedisStorage(uri, implementation="redispy")
        except TypeError:  # an older `limits` without the option
            storage = RedisStorage(uri)
        _redis_limiter = MovingWindowRateLimiter(storage)
        _redis_url_in_use = s.redis_url
    return _redis_limiter


async def _hit(limit_str: str, *identifiers: str) -> tuple[bool, int]:
    """Counts one request. Returns (allowed, retry_after_seconds)."""
    global _redis_down_until
    item = parse(limit_str)
    try:
        limiter = _redis()  # building the Redis storage can fail (driver missing, bad URL): fall back, never 500
    except Exception:  # noqa: BLE001
        _redis_down_until = time.monotonic() + get_settings().redis_retry_after_seconds
        logger.warning("Rate limiter: could not set up Redis, using the in-process limiter.", exc_info=True)
        limiter = None
    if limiter is not None:
        try:
            ok = await limiter.hit(item, *identifiers)
            if ok:
                return True, 0
            stats = await limiter.get_window_stats(item, *identifiers)
            return False, max(1, int(stats.reset_time - time.time()) + 1)
        except Exception:  # noqa: BLE001 - Redis down/misconfigured: fall back to the per-process limiter
            _redis_down_until = time.monotonic() + get_settings().redis_retry_after_seconds
            logger.warning("Rate limiter: Redis unavailable, using the in-process limiter.", exc_info=True)
    return _memory.hit(item, *identifiers)


def reset_rate_limits() -> None:
    """Forget all in-process counters (tests)."""
    _memory.reset()


def rate_limit(name: str, getter: Callable[[Settings], str], *, per_user: bool = False):
    """A dependency that rejects with 429 + Retry-After once the caller exceeds the configured limit."""

    async def dependency(request: Request) -> None:
        s = get_settings()
        if not s.rate_limit_enabled:
            return
        user_id = peek_user_id(request) if per_user else None
        who = f"user:{user_id}" if user_id else f"ip:{client_ip(request) or 'unknown'}"
        allowed, retry_after = await _hit(getter(s), "rl", name, who)
        if not allowed:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Please slow down and try again shortly.",
                headers={"Retry-After": str(retry_after)},
            )

    return dependency


async def within_limit(name: str, limit_str: str, identifier: str) -> bool:
    """Counts one hit against a named bucket keyed by something other than the caller (e.g. a hashed e-mail
    address) and says whether it is still within the limit. Always True when rate limiting is disabled."""
    if not get_settings().rate_limit_enabled:
        return True
    allowed, _retry = await _hit(limit_str, "rl", name, identifier)
    return allowed
