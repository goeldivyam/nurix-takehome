from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Literal

from app.audit.emitter import emit_audit
from app.audit.events import AuditEvent
from app.persistence.repositories import CallRepo, WebhookInboxRepo
from app.state import machine as state

if TYPE_CHECKING:
    from app.deps import Deps

logger = logging.getLogger(__name__)

_RowOutcome = Literal["applied", "stale", "empty", "error"]


async def process_pending_inbox(deps: Deps) -> int:
    # Bounded loop-drain. Ingest spawns this per request (the tracked task
    # pattern in backend-conventions) and a safety-net loop also runs it on
    # a timer. The cap keeps tail latency predictable under a burst — a
    # saturated inbox doesn't monopolize the event loop.
    cap = deps.settings.webhook_processor_batch_max
    processed = 0
    applied_any = False
    for _ in range(cap):
        outcome = await _process_one_row(deps)
        if outcome == "empty":
            break
        if outcome == "applied":
            applied_any = True
        processed += 1

    # Only notify when a transition actually landed. Stale no-ops don't free
    # capacity and would spam the scheduler wake for no gain.
    if applied_any:
        deps.wake.notify()
    return processed


async def _process_one_row(deps: Deps) -> _RowOutcome:
    # Every mutation below runs on a single connection inside one transaction.
    # If `mark_processed` or the transition raises, the txn rolls back and the
    # inbox row stays unclaimed for the next drain cycle.
    try:
        async with deps.pools.scheduler.acquire() as conn, conn.transaction():
            row = await WebhookInboxRepo.claim_unprocessed_one(conn)
            if row is None:
                return "empty"

            event = deps.parse_event_fn(row.payload)
            call = await CallRepo.get_by_provider_call_id(conn, event.provider_call_id)
            if call is None:
                # Unknown provider_call_id: the event is for a call we don't
                # own (e.g. late-arriving echo from a previous deploy). Drop
                # it with a forensic audit row so operators can trace it.
                await emit_audit(
                    conn,
                    AuditEvent(
                        event_type="WEBHOOK_IGNORED_STALE",
                        reason="unknown provider_call_id",
                        extra={
                            "provider_event_id": event.provider_event_id,
                            "provider_call_id": event.provider_call_id,
                        },
                    ),
                )
                await WebhookInboxRepo.mark_processed(conn, row.id)
                return "stale"

            result = await state.transition(
                conn,
                call_id=call.id,
                expected_status=call.status,
                new_status=event.status_enum,
                expected_epoch=call.attempt_epoch,
                event_type="TRANSITION",
                reason=f"webhook {event.status_enum.value}",
                extra={
                    "provider_event_id": event.provider_event_id,
                    "provider_call_id": event.provider_call_id,
                },
            )
            if result.is_no_op():
                # CAS missed: the row moved on us (reordered webhook, or the
                # reclaim sweep bumped the epoch first). Persist a stale
                # audit row so "lost webhook" debugging stays forensically
                # answerable straight off the audit table.
                await emit_audit(
                    conn,
                    AuditEvent(
                        event_type="WEBHOOK_IGNORED_STALE",
                        campaign_id=call.campaign_id,
                        call_id=call.id,
                        reason=f"CAS no-op: expected {call.status}/{call.attempt_epoch}",
                        extra={
                            "expected_status": call.status,
                            "expected_epoch": call.attempt_epoch,
                            "event_status": event.status_enum.value,
                            "provider_event_id": event.provider_event_id,
                        },
                    ),
                )
                await WebhookInboxRepo.mark_processed(conn, row.id)
                return "stale"

            await WebhookInboxRepo.mark_processed(conn, row.id)
            return "applied"
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("webhook processor row failed")
        return "error"


async def webhook_inbox_safety_net_loop(deps: Deps) -> None:
    # Secondary drain for rows orphaned between ingest's commit-then-spawn
    # and the per-request task (event-loop saturation, crash, etc.). Uses
    # the scheduler safety-net cadence so worst-case webhook latency is
    # bounded by that same value.
    interval = deps.settings.scheduler_safety_net_seconds
    while True:
        try:
            await process_pending_inbox(deps)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("webhook inbox safety-net crashed")
        await asyncio.sleep(interval)
