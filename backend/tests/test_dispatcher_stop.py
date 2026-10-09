"""Regression: `Dispatcher.stop()` must return even when a loop's cancellation is swallowed.

Same failure mode as `tests/test_chat_runner_stop.py`: a cancel that lands inside a DB driver call can come back as an
ordinary exception; the loops' `except Exception` then swallowed it and carried on forever, so `stop()` (awaiting the
loop task) never returned. `_stopping` is the real exit condition, the cancel only makes it prompt.
"""

from __future__ import annotations

import asyncio

from app.pipeline.dispatcher import Dispatcher
from tests.fakes import FakeAwsJobs, FakeBedrockClient, FakeJevScoreClient, master_converse_fn


def _dispatcher() -> Dispatcher:
    return Dispatcher(
        FakeAwsJobs(), FakeBedrockClient(converse_fn=master_converse_fn()), FakeJevScoreClient(),
        max_concurrent=1, poll_seconds=0.01,
    )


async def test_stop_returns_when_the_claim_loop_swallows_its_cancellation(monkeypatch) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "ai_eval_inline", False, raising=False)
    d = _dispatcher()
    in_tick = asyncio.Event()
    ticks = 0

    async def no_recover() -> int:
        return 0

    async def tick_that_converts_cancel_into_an_error():
        nonlocal ticks
        ticks += 1
        in_tick.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise RuntimeError("the driver turned the cancel into an ordinary error") from None
        return []

    monkeypatch.setattr(d, "recover", no_recover)
    monkeypatch.setattr(d, "tick", tick_that_converts_cancel_into_an_error)

    await d.start()
    await asyncio.wait_for(in_tick.wait(), 10)
    await asyncio.wait_for(d.stop(), 10)  # used to wait forever
    assert d._loop_task is None
    assert ticks == 1  # the loop ended instead of ticking on


async def test_stop_returns_when_the_heartbeat_loop_swallows_its_cancellation(monkeypatch) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "ai_eval_inline", False, raising=False)
    monkeypatch.setattr(get_settings(), "lease_heartbeat_seconds", 0, raising=False)
    d = _dispatcher()
    in_beat = asyncio.Event()
    beats = 0

    async def no_recover() -> int:
        return 0

    async def idle_tick():
        return []

    async def beat_that_converts_cancel_into_an_error() -> int:
        nonlocal beats
        beats += 1
        in_beat.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise RuntimeError("the driver turned the cancel into an ordinary error") from None
        return 0

    monkeypatch.setattr(d, "recover", no_recover)
    monkeypatch.setattr(d, "tick", idle_tick)
    monkeypatch.setattr(d, "heartbeat", beat_that_converts_cancel_into_an_error)

    await d.start()
    await asyncio.wait_for(in_beat.wait(), 10)
    await asyncio.wait_for(d.stop(), 10)
    assert d._heartbeat_task is None
    assert beats == 1
