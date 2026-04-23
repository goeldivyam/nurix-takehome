from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.config import Settings
    from app.persistence.pools import Pools
    from app.provider.base import TelephonyProvider
    from app.provider.types import ProviderEvent
    from app.scheduler.wake import SchedulerWake


ParseEventFn = Callable[[dict[str, Any]], "ProviderEvent"]
VerifySignatureFn = Callable[[dict[str, str], bytes], bool]


@dataclass(slots=True)
class Deps:
    # Single container of long-lived dependencies, built once in the FastAPI
    # lifespan and injected into scheduler, reclaim, webhook processor, and
    # the HTTP routes. Using a dataclass (not Pydantic) so identity-based
    # methods (`pool.acquire`, `wake.notify`) survive the boundary.
    settings: Settings
    pools: Pools
    provider: TelephonyProvider
    wake: SchedulerWake
    tracked_tasks: set[asyncio.Task[Any]]
    parse_event_fn: ParseEventFn
    verify_signature_fn: VerifySignatureFn
