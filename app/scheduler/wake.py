from __future__ import annotations

import asyncio


class SchedulerWake:
    # Single asyncio.Event wrapped with the canonical tick-loop shape:
    #   await wake.wait(timeout=safety_net)
    #   wake.clear()   # BEFORE tick; captures notify() arriving during tick
    #   await tick()
    # State + webhook processor inject this and call notify() after every
    # transition that may free capacity. Safety-net timeout handles the rare
    # case where every campaign is quiet.

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def notify(self) -> None:
        self._event.set()

    def clear(self) -> None:
        self._event.clear()

    async def wait(self, timeout: float | None = None) -> bool:  # noqa: ASYNC109
        # `timeout` on the port is part of the loop contract in CLAUDE.md:
        # `await wake.wait(timeout=safety_net_seconds)`. asyncio.wait_for is the
        # right primitive here; ASYNC109 is suppressed intentionally.
        if timeout is None:
            await self._event.wait()
            return True
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False
