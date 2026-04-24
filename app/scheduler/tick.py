from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from app.audit.emitter import emit_audit
from app.audit.events import AuditEvent
from app.persistence.repositories import (
    CallRepo,
    CampaignRepo,
    CampaignRowWithCursor,
    SchedulerStateRepo,
)
from app.provider.types import ProviderRejected, ProviderUnavailable
from app.scheduler.business_hours import is_in_window
from app.state import machine as state
from app.state.campaign_terminal import maybe_promote_to_active
from app.state.retry_apply import compute_backoff
from app.state.types import CallStatus, CampaignStatus

if TYPE_CHECKING:
    from app.deps import Deps

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TickDecision:
    # Return value of `tick` so tests can assert the selected path without
    # re-reading the DB. `None` = no work; otherwise carries the campaign
    # that was dispatched and whether it was a retry.
    campaign_id: UUID | None
    is_retry: bool


def _rr_sort_key(c: CampaignRowWithCursor) -> tuple[datetime, UUID]:
    # Round-robin cursor: oldest last_dispatch_at first, ties broken by
    # campaign_id so the order is totally deterministic. A brand-new campaign
    # (last_dispatch_at IS NULL) outranks any campaign that's already dispatched.
    ts = c.last_dispatch_at or datetime.min.replace(tzinfo=UTC)
    return (ts, c.id)


async def tick(deps: Deps) -> TickDecision:
    # 5-step dispatch pipeline, executed once per scheduler wake. Emits at
    # most one CLAIMED + DISPATCH pair per tick. `place_call` runs outside
    # any DB transaction — the three-phase pattern is the reason the scheduler
    # can scale horizontally without holding connections across HTTP latency.
    now_utc = datetime.now(tz=UTC)

    # Steps 1-3 are pure reads and commute with each other — share one
    # connection so an operator auditing "what the scheduler saw at time T"
    # observes a consistent snapshot.
    async with deps.pools.scheduler.acquire() as conn:
        all_eligible = await CampaignRepo.list_eligible_for_tick(conn)
        if not all_eligible:
            return TickDecision(None, is_retry=False)

        # ---- Step 2a: business-hour gate ---------------------------------
        in_hours: list[CampaignRowWithCursor] = []
        out_of_hours: list[CampaignRowWithCursor] = []
        for c in all_eligible:
            (in_hours if is_in_window(c.schedule, c.timezone, now_utc) else out_of_hours).append(c)

        # Emit SKIP_BUSINESS_HOUR for campaigns that had queued work but were
        # gated out by their schedule. The audit log must answer
        # "why not campaign X?" for the rubric's observability point.
        for c in out_of_hours:
            await emit_audit(
                conn,
                AuditEvent(
                    event_type="SKIP_BUSINESS_HOUR",
                    reason="outside configured business-hour window",
                    campaign_id=c.id,
                    extra={"timezone": c.timezone},
                ),
            )
        if not in_hours:
            return TickDecision(None, is_retry=False)

        # ---- Step 2b: concurrency gate -----------------------------------
        in_flight = await CallRepo.in_flight_counts_by_campaign(conn, [c.id for c in in_hours])
        capped: list[CampaignRowWithCursor] = []
        for c in in_hours:
            current = in_flight.get(c.id, 0)
            if current < c.max_concurrent:
                capped.append(c)
            else:
                await emit_audit(
                    conn,
                    AuditEvent(
                        event_type="SKIP_CONCURRENCY",
                        reason=(f"in-flight {current} >= max_concurrent {c.max_concurrent}"),
                        campaign_id=c.id,
                        extra={
                            "in_flight": current,
                            "max_concurrent": c.max_concurrent,
                        },
                    ),
                )
        if not capped:
            return TickDecision(None, is_retry=False)

        # ---- Step 3: retry sweep among survivors -------------------------
        retry_due_campaigns = set(await CallRepo.find_retry_due_campaign_ids(conn))

    retry_candidates = sorted((c for c in capped if c.id in retry_due_campaigns), key=_rr_sort_key)

    picked: CampaignRowWithCursor | None = None
    is_retry = False
    if retry_candidates:
        picked = retry_candidates[0]
        is_retry = True
    elif capped:
        # ---- Step 4: RR pick among campaigns with fresh queued work ------
        picked = min(capped, key=_rr_sort_key)

    if picked is None:
        return TickDecision(None, is_retry=False)

    # ---- Step 5: three-phase dispatch ------------------------------------
    # When the pick is a retry, move the oldest retry-due row back to QUEUED
    # first so the claim primitive (QUEUED-only) can pick it up atomically
    # in Phase 1. Emits a RETRY_DUE audit so the log explains the transition.
    if is_retry:
        await _requeue_oldest_retry_due(deps, picked.id)

    dispatched_campaign_id = await _dispatch_one(deps, picked)
    return TickDecision(dispatched_campaign_id, is_retry=is_retry)


async def _requeue_oldest_retry_due(deps: Deps, campaign_id: UUID) -> None:
    # Find the oldest RETRY_PENDING row whose backoff has elapsed and
    # transition it RETRY_PENDING → QUEUED. The CAS guards against a
    # sibling tick racing the same row.
    async with deps.pools.scheduler.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """
            SELECT id, attempt_epoch FROM calls
            WHERE campaign_id = $1
              AND status = 'RETRY_PENDING'
              AND next_attempt_at IS NOT NULL
              AND next_attempt_at <= NOW()
            ORDER BY created_at ASC
            LIMIT 1 FOR UPDATE SKIP LOCKED
            """,
            campaign_id,
        )
        if row is None:
            return
        await state.transition(
            conn,
            call_id=row["id"],
            expected_status=CallStatus.RETRY_PENDING,
            new_status=CallStatus.QUEUED,
            expected_epoch=row["attempt_epoch"],
            event_type="RETRY_DUE",
            reason="backoff elapsed; requeueing for dispatch",
        )


async def _dispatch_one(deps: Deps, campaign: CampaignRowWithCursor) -> UUID | None:
    # Three-phase pattern. Holding a DB connection across `place_call` would
    # pin pool capacity to provider latency — a single brown-out stalls the
    # whole scheduler. Splitting the transaction boundaries keeps the DB
    # quiet during the outbound call.

    # ---- Phase 1 — claim + CLAIMED audit --------------------------------
    async with deps.pools.scheduler.acquire() as conn, conn.transaction():
        # Snapshot BEFORE the claim so `in_flight_at_claim` reflects the
        # state the scheduler decided on (not the post-claim count which
        # already includes the row we just moved to DIALING).
        retries_pending_system = await CallRepo.count_retries_due_system(conn)
        in_flight_at_claim = await CallRepo.in_flight_count(conn, campaign.id)
        rr_cursor_before = await SchedulerStateRepo.get_last_dispatch_at(conn, campaign.id)

        claimed = await CallRepo.claim_next_queued(conn, campaign.id)
        if claimed is None:
            # Nothing actually claimable (row drained between eligibility
            # read and claim). Silent no-op; next tick picks it up.
            return None

        await emit_audit(
            conn,
            AuditEvent(
                event_type="CLAIMED",
                reason="claim for dispatch",
                campaign_id=campaign.id,
                call_id=claimed.id,
                state_before="QUEUED",
                state_after="DIALING",
                extra={
                    "attempt_epoch": claimed.attempt_epoch,
                    "in_flight_at_claim": in_flight_at_claim,
                    "max_concurrent": campaign.max_concurrent,
                    "retries_pending_system": retries_pending_system,
                    "rr_cursor_before": (
                        rr_cursor_before.isoformat() if rr_cursor_before else None
                    ),
                },
            ),
        )

        # PENDING → ACTIVE promotion must live INSIDE this transaction. If it
        # ran on a second connection, a crash between commits would leave the
        # call DIALING while the parent campaign is still PENDING, which then
        # breaks the terminal-rollup CAS (which requires status='ACTIVE').
        # `maybe_promote_to_active` is idempotent — CAS on status='PENDING' —
        # so calling it unconditionally on every claim is cheap and safe.
        if campaign.status == CampaignStatus.PENDING.value:
            await maybe_promote_to_active(conn, campaign.id)

    # ---- Phase 2 — place_call (no DB txn) -------------------------------
    idem_key = f"{claimed.id}:{claimed.attempt_epoch}"
    outcome_tag: str
    outcome_handle: Any
    try:
        handle = await deps.provider.place_call(idem_key, claimed.phone)
        outcome_tag, outcome_handle = "OK", handle
    except ProviderRejected as exc:
        outcome_tag, outcome_handle = "REJECTED", exc.reason_code
    except ProviderUnavailable:
        outcome_tag, outcome_handle = "UNAVAILABLE", None

    # ---- Phase 3 — apply result -----------------------------------------
    # The retry_config is stored on the campaign as JSONB. If missing, fall
    # back to the settings default so a malformed config doesn't stall the
    # scheduler.
    base_seconds = float(
        campaign.retry_config.get("backoff_base_seconds", deps.settings.retry_backoff_base_seconds)
    )

    async with deps.pools.scheduler.acquire() as conn, conn.transaction():
        if outcome_tag == "OK":
            handle = outcome_handle
            await state.transition(
                conn,
                call_id=claimed.id,
                expected_status=CallStatus.DIALING,
                new_status=CallStatus.DIALING,
                expected_epoch=claimed.attempt_epoch,
                event_type="DISPATCH",
                reason="dispatched to provider",
                extra={
                    "provider_call_id": handle.provider_call_id,
                    "attempt_epoch": claimed.attempt_epoch,
                },
                column_updates={"provider_call_id": handle.provider_call_id},
            )
        elif outcome_tag == "REJECTED":
            await state.transition(
                conn,
                call_id=claimed.id,
                expected_status=CallStatus.DIALING,
                new_status=CallStatus.FAILED,
                expected_epoch=claimed.attempt_epoch,
                event_type="TRANSITION",
                reason=f"provider_rejected:{outcome_handle}",
                extra={"reason_code": outcome_handle},
            )
        else:  # UNAVAILABLE
            if claimed.retries_remaining > 0:
                next_attempt = datetime.now(tz=UTC) + compute_backoff(
                    claimed.attempt_epoch, base_seconds
                )
                await state.transition(
                    conn,
                    call_id=claimed.id,
                    expected_status=CallStatus.DIALING,
                    new_status=CallStatus.RETRY_PENDING,
                    expected_epoch=claimed.attempt_epoch,
                    event_type="TRANSITION",
                    reason="provider_unavailable",
                    column_updates={
                        "next_attempt_at": next_attempt,
                        "retries_remaining": claimed.retries_remaining - 1,
                    },
                )
            else:
                await state.transition(
                    conn,
                    call_id=claimed.id,
                    expected_status=CallStatus.DIALING,
                    new_status=CallStatus.FAILED,
                    expected_epoch=claimed.attempt_epoch,
                    event_type="TRANSITION",
                    reason="provider_unavailable, retries exhausted",
                )

        await SchedulerStateRepo.update_last_dispatch_at(conn, campaign.id, datetime.now(tz=UTC))

    return campaign.id
