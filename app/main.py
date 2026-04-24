from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routers.audit import router as audit_router
from app.api.routers.calls import router as calls_router
from app.api.routers.campaigns import router as campaigns_router
from app.api.routers.webhooks import router as webhooks_router
from app.api.webhooks_ingest import handle_webhook_ingest
from app.config import Settings
from app.deps import Deps
from app.persistence.pools import close_pools, create_pools
from app.provider.mock import MockProvider, parse_event, verify_signature
from app.scheduler.loop import scheduler_loop
from app.scheduler.reclaim import stuck_reclaim_sweep_loop
from app.scheduler.wake import SchedulerWake
from app.scheduler.webhook_processor import webhook_inbox_safety_net_loop

logger = logging.getLogger(__name__)


def _spawn(
    tracked: set[asyncio.Task[Any]],
    coro: Any,
    *,
    name: str,
) -> asyncio.Task[Any]:
    # Creates a background task, registers it in `tracked`, and logs any
    # unhandled exception through the done-callback. The tracked set
    # prevents Python's GC from collecting the task before it runs and
    # lets the lifespan cancel every daemon cleanly on shutdown.
    task = asyncio.create_task(coro, name=name)
    tracked.add(task)

    def _on_done(t: asyncio.Task[Any]) -> None:
        tracked.discard(t)
        if not t.cancelled():
            exc = t.exception()
            if exc is not None:
                logger.error("daemon %s crashed", name, exc_info=exc)

    task.add_done_callback(_on_done)
    return task


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Single entry point for every long-lived resource. Pools, provider,
    # scheduler wake, tracked-task set, and the three background daemons
    # are created here in order and torn down in reverse — no module-level
    # singletons, no import-time I/O.
    settings = Settings()
    pools = await create_pools(settings)
    wake = SchedulerWake()
    tracked: set[asyncio.Task[Any]] = set()

    # Resolve `deps` at sink call time. The provider's simulated events
    # land through `handle_webhook_ingest`, which depends on `deps` —
    # constructing a closure over `deps` directly would require a forward
    # reference because the provider itself is part of `deps`.
    async def event_sink(payload: dict[str, Any]) -> None:
        live_deps: Deps = app.state.deps
        await handle_webhook_ingest(
            live_deps,
            provider="mock",
            payload=payload,
            raw_body=b"",
            headers={},
        )

    provider = MockProvider(settings, event_sink=event_sink)
    deps = Deps(
        settings=settings,
        pools=pools,
        provider=provider,
        wake=wake,
        tracked_tasks=tracked,
        parse_event_fn=parse_event,
        verify_signature_fn=verify_signature,
    )
    app.state.deps = deps
    app.state.wake = wake
    app.state.tracked_tasks = tracked

    _spawn(tracked, scheduler_loop(deps, wake), name="scheduler")
    _spawn(tracked, stuck_reclaim_sweep_loop(deps), name="reclaim")
    _spawn(tracked, webhook_inbox_safety_net_loop(deps), name="inbox-safety")

    try:
        yield
    finally:
        for t in list(tracked):
            t.cancel()
        if tracked:
            await asyncio.gather(*tracked, return_exceptions=True)
        await provider.aclose()
        await close_pools(pools)


app = FastAPI(title="Nurix Voice Campaign", version="0.1.0", lifespan=lifespan)

app.include_router(campaigns_router)
app.include_router(calls_router)
app.include_router(audit_router)
app.include_router(webhooks_router)


# /ui is pre-mounted when the frontend bundle exists. P4 lands the files;
# until then the mount simply no-ops.
if os.path.isdir("frontend"):
    app.mount("/ui", StaticFiles(directory="frontend", html=True), name="ui")


@app.get("/health")
async def health() -> dict[str, Any]:
    # Reflects pool sizing so an operator can diagnose "is the process
    # actually serving?" separately from "can it reach Postgres?" The pool
    # fields are gated on lifespan state: before lifespan runs (cold
    # startup) we return a degraded shape rather than raising.
    pools_state = getattr(app.state, "deps", None)
    if pools_state is None:
        return {"status": "ok", "pools": None}
    pools = pools_state.pools
    return {
        "status": "ok",
        "pools": {
            "api": {"size": pools.api.get_size(), "idle": pools.api.get_idle_size()},
            "scheduler": {
                "size": pools.scheduler.get_size(),
                "idle": pools.scheduler.get_idle_size(),
            },
            "webhook": {
                "size": pools.webhook.get_size(),
                "idle": pools.webhook.get_idle_size(),
            },
        },
    }


# Debug endpoints (age-dialing, etc.) are gated behind a setting so they
# never ship to production by accident. The router is included only when
# DEBUG_ENDPOINTS_ENABLED=true; the handler itself also re-checks the flag.
if Settings().debug_endpoints_enabled:
    from app.api.routers.debug import router as debug_router

    app.include_router(debug_router)
