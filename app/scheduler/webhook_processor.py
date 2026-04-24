from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Literal

from app.audit.emitter import emit_audit
from app.audit.events import AuditEvent
from app.persistence.repositories import CallRepo, CampaignRepo, WebhookInboxRepo
from app.state import machine as state
from app.state.retry_apply import apply_retryable_outcome
from app.state.types import TERMINAL_CALL_STATUSES, CallStatus

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

            # Suppress same-state noise: the mock emits a leading DIALING event
            # after place_call, but the scheduler already owns that state from
            # Phase 1. Emitting a same-state TRANSITION audit row would pollute
            # the visualization with no-signal entries. Real providers can
            # also re-fire identical status events on retry; collapse them
            # here so the audit log only carries actual transitions.
            event_status = CallStatus(event.status_enum)
            current_status = CallStatus(call.status)
            if event_status == current_status:
                await WebhookInboxRepo.mark_processed(conn, row.id)
                return "stale"

            # Terminal disposition: route through the shared retry-apply helper
            # so NO_ANSWER / BUSY land in RETRY_PENDING if the budget allows,
            # matching the behavior the scheduler's own Phase 3 path uses.
            # Rubric #5: retries are driven off provider outcomes, not just
            # scheduler-side ProviderUnavailable.
            if event_status in TERMINAL_CALL_STATUSES:
                # Route the retry-config read through the repo so persistence
                # stays the single owner of JSONB decoding. Falling back to
                # the settings default (never 0) on a missing/malformed key
                # keeps backoff semantics aligned with tick's Phase 3 path.
                campaign_row = await CampaignRepo.get(conn, call.campaign_id)
                retry_config = campaign_row.retry_config if campaign_row else {}
                base_raw = retry_config.get(
                    "backoff_base_seconds",
                    deps.settings.retry_backoff_base_seconds,
                )
                base_seconds = (
                    float(base_raw)
                    if isinstance(base_raw, int | float | str)
                    else float(deps.settings.retry_backoff_base_seconds)
                )
                result = await apply_retryable_outcome(
                    conn,
                    call_id=call.id,
                    expected_status=current_status,
                    expected_epoch=call.attempt_epoch,
                    retries_remaining=call.retries_remaining,
                    outcome=event_status,
                    backoff_base_seconds=base_seconds,
                    reason_prefix="webhook",
                )
                # Fall through to the shared no-op / applied handling below so
                # stale-CAS races against a reclaim bump land a WEBHOOK_IGNORED_STALE
                # row rather than silently dropping.
            else:
                # Intermediate transition (e.g. IN_PROGRESS): write through directly.
                result = await state.transition(
                    conn,
                    call_id=call.id,
                    expected_status=current_status,
                    new_status=event_status,
                    expected_epoch=call.attempt_epoch,
                    event_type="TRANSITION",
                    reason=f"webhook {event_status.value}",
                    extra={
                        "provider_event_id": event.provider_event_id,
                        "provider_call_id": event.provider_call_id,
                    },
                )
            if result.is_no_op():
                # Two shapes of no-op, distinguished by the state machine's
                # `rejected_reason`:
                #   - terminal-regression: the row is already terminal and an
                #     intermediate event arrived after it (provider re-fires
                #     IN_PROGRESS after COMPLETED/FAILED/NO_ANSWER/BUSY).
                #     The state machine refused the edge up-front; we record
                #     the race with a `terminal-wins` reason.
                #   - plain CAS no-op: the SQL CAS matched zero rows because
                #     the status or epoch moved on us (reordered webhook,
                #     reclaim bumped the epoch first). Record with the
                #     expected_status / expected_epoch so "lost webhook"
                #     debugging stays forensically answerable off the audit
                #     table alone.
                reason: str
                extra: dict[str, object]
                if result.is_terminal_regression():
                    reason = f"terminal-wins: {current_status.value} ← {event_status.value}"
                    extra = {
                        "current_status": current_status.value,
                        "event_status": event_status.value,
                        "provider_event_id": event.provider_event_id,
                    }
                else:
                    reason = f"CAS no-op: expected {call.status}/{call.attempt_epoch}"
                    extra = {
                        "expected_status": call.status,
                        "expected_epoch": call.attempt_epoch,
                        "event_status": event.status_enum.value,
                        "provider_event_id": event.provider_event_id,
                    }
                await emit_audit(
                    conn,
                    AuditEvent(
                        event_type="WEBHOOK_IGNORED_STALE",
                        campaign_id=call.campaign_id,
                        call_id=call.id,
                        reason=reason,
                        extra=extra,
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
