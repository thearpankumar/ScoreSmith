"""Adaptive concurrency limiter for the master model's Bedrock calls.

Several evaluations score at once and share one Bedrock quota. A fixed number of in-flight calls is
either too timid (slow batches) or too bold (a throttling storm). This limiter is AIMD: it starts at
the configured maximum, halves its allowance when Bedrock says "too many requests" (at most once per
cooldown, so one burst of throttles does not collapse it to 1), and grows back by one after a run of
successes. The result is the highest sustained rate the account's quota actually allows.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.config import get_settings


class AdaptiveLimiter:
    def __init__(self, maximum: int, minimum: int = 1, grow_after: int = 6, cooldown: float = 3.0) -> None:
        self.maximum = max(1, maximum)
        self.minimum = max(1, min(minimum, self.maximum))
        self._grow_after = max(1, grow_after)
        self._cooldown = cooldown
        self._limit = self.maximum
        self._in_flight = 0
        self._successes = 0
        self._last_cut = float("-inf")
        self._loop: asyncio.AbstractEventLoop | None = None
        self._cond: asyncio.Condition | None = None

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def _condition(self) -> asyncio.Condition:
        # asyncio primitives are bound to one event loop; tests (and reloads) create several.
        loop = asyncio.get_running_loop()
        if self._cond is None or self._loop is not loop:
            self._loop, self._cond, self._in_flight = loop, asyncio.Condition(), 0
        return self._cond

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        cond = self._condition()
        async with cond:
            await cond.wait_for(lambda: self._in_flight < self._limit)
            self._in_flight += 1
        try:
            yield
        finally:
            async with cond:
                self._in_flight -= 1
                cond.notify_all()

    async def on_success(self) -> None:
        cond = self._condition()
        async with cond:
            self._successes += 1
            if self._successes >= self._grow_after and self._limit < self.maximum:
                self._limit += 1
                self._successes = 0
                cond.notify_all()

    async def on_throttle(self) -> None:
        cond = self._condition()
        async with cond:
            self._successes = 0
            now = time.monotonic()
            if now - self._last_cut >= self._cooldown:
                self._limit = max(self.minimum, self._limit // 2)
                self._last_cut = now


_limiter: AdaptiveLimiter | None = None


def get_master_limiter() -> AdaptiveLimiter:
    """Process-wide limiter, sized from `bedrock_max_concurrency`."""
    global _limiter
    maximum = get_settings().bedrock_max_concurrency
    if _limiter is None or _limiter.maximum != maximum:
        _limiter = AdaptiveLimiter(maximum)
    return _limiter
