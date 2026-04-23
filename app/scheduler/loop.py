from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from app.scheduler.tick import tick

if TYPE_CHECKING:
    from app.deps import Deps
    from app.scheduler.wake import SchedulerWake


logger = logging.getLogger(__name__)


async def scheduler_loop(deps: Deps, wake: SchedulerWake) -> None:
    # Canonical loop shape per CLAUDE.md — never lose a wakeup:
    #   wait (with safety-net timeout)
    #   clear BEFORE tick so a notify during tick is captured for the next iter
    #   tick
    safety_net = deps.settings.scheduler_safety_net_seconds
    while True:
        try:
            await wake.wait(timeout=safety_net)
            wake.clear()
            await tick(deps)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never let a transient tick error crash the daemon. Log, pause
            # briefly so we don't thrash if the DB is flapping, and continue.
            logger.exception("scheduler tick failed")
            await asyncio.sleep(safety_net)
