"""Regression: `ChatTurnRunner.stop()` must return even when the supervisor's cancellation is swallowed.

Seen as a rare hang of the whole test session (the app lifespan never finished shutting down): the supervisor was
cancelled while a DB call was in flight, the driver turned the `CancelledError` into an ordinary exception, the loop's
`except Exception` swallowed it and carried on forever, and `stop()` awaited it for good.
"""

from __future__ import annotations

import asyncio

from app.api.v1.chat import ChatTurnRunner


async def test_stop_returns_when_the_supervisor_swallows_its_cancellation(monkeypatch) -> None:
    runner = ChatTurnRunner()
    in_claim = asyncio.Event()
    ticks = 0

    async def no_reap() -> int:
        return 0

    async def no_supervise() -> None:
        return None

    async def claim_that_converts_cancel_into_an_error() -> int:
        nonlocal ticks
        ticks += 1
        in_claim.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise RuntimeError("the driver turned the cancel into an ordinary error") from None
        return 0

    monkeypatch.setattr(runner, "reap", no_reap)
    monkeypatch.setattr(runner, "supervise_once", no_supervise)
    monkeypatch.setattr(runner, "claim", claim_that_converts_cancel_into_an_error)

    await runner.start()
    await asyncio.wait_for(in_claim.wait(), 10)
    await asyncio.wait_for(runner.stop(), 10)  # used to wait forever
    assert runner._supervisor is None
    assert ticks == 1  # the loop ended instead of ticking on
