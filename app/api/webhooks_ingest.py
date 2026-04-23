from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import HTTPException

from app.deps import Deps
from app.persistence.repositories import WebhookInboxRepo
from app.scheduler.webhook_processor import process_pending_inbox

logger = logging.getLogger(__name__)


async def handle_webhook_ingest(
    deps: Deps,
    *,
    provider: str,
    payload: dict[str, Any],
    raw_body: bytes,
    headers: dict[str, str],
) -> dict[str, bool]:
    # 1) Signature verification runs BEFORE any DB write. The adapter's
    #    `verify_signature` is bound into `deps` at lifespan startup; the
    #    mock is trust-by-default, real adapters HMAC-verify.
    if not deps.verify_signature_fn(headers, raw_body):
        raise HTTPException(status_code=401, detail="bad signature")

    event_id = payload.get("provider_event_id")
    if not event_id or not isinstance(event_id, str):
        raise HTTPException(status_code=400, detail="missing provider_event_id")

    # 2) Inbox insert runs inside its own transaction; the UNIQUE
    #    (provider, provider_event_id) index makes this idempotent and the
    #    repo's ON CONFLICT ... DO UPDATE preserves RETURNING id on replay.
    async with deps.pools.webhook.acquire() as conn, conn.transaction():
        await WebhookInboxRepo.insert(
            conn,
            provider=provider,
            provider_event_id=event_id,
            payload=payload,
            headers=headers,
        )

    # 3) AFTER commit, spawn the processor as a tracked daemon. Spawning
    #    before commit would race: the processor could dequeue before the
    #    new row is visible in its snapshot and silently no-op, leaving
    #    the row for the periodic safety-net sweep. The tracked-task set
    #    prevents GC from collecting the task before it runs and lets the
    #    lifespan cancel it cleanly on shutdown.
    task = asyncio.create_task(
        process_pending_inbox(deps),
        name=f"inbox-{event_id[:8]}",
    )
    deps.tracked_tasks.add(task)

    def _done(t: asyncio.Task[Any]) -> None:
        deps.tracked_tasks.discard(t)
        if not t.cancelled():
            exc = t.exception()
            if exc is not None:
                logger.error("webhook processor task failed", exc_info=exc)

    task.add_done_callback(_done)

    return {"received": True}
