from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from app.state import machine as state
from app.state.retry_classification import RetryOutcome, classify
from app.state.types import CallStatus

if TYPE_CHECKING:
    import asyncpg


def compute_backoff(attempt_epoch: int, base_seconds: float) -> timedelta:
    # Index the exponent by attempt count, not the shrinking retries-remaining
    # budget, so the first retry always sleeps ~base, the second ~2*base, etc.
    exponent = 0 if attempt_epoch < 1 else attempt_epoch - 1
    jitter = 1.0 + random.uniform(-0.2, 0.2)  # noqa: S311 -- backoff jitter, not crypto
    return timedelta(seconds=base_seconds * (2**exponent) * jitter)


async def apply_retryable_outcome(
    conn: asyncpg.Connection,
    *,
    call_id: UUID,
    expected_status: CallStatus,
    expected_epoch: int,
    retries_remaining: int,
    outcome: CallStatus,
    backoff_base_seconds: float,
    reason_prefix: str,
) -> state.TransitionResult:
    # Shared retry-or-terminal branch used by both the scheduler's Phase 3
    # dispatch path and the webhook processor. Returns the TransitionResult
    # so callers can tell a CAS no-op (race) apart from a real apply and
    # emit the appropriate audit row.
    #
    # Contract:
    #   COMPLETED                    -> terminal, no retry.
    #   FAILED (explicit terminal)   -> terminal, no retry (rejection case).
    #   NO_ANSWER / BUSY             -> retry if budget remains, else terminal
    #                                    FAILED with "retries exhausted" reason.
    #
    # NO_ANSWER / BUSY exhaust collapses into FAILED so the external
    # /calls/{id} status mapping reports "failed" consistently — operators
    # don't need to learn the finer internal vocabulary for the happy path.
    retry_decision = classify(outcome)

    if retry_decision is RetryOutcome.TERMINAL:
        return await state.transition(
            conn,
            call_id=call_id,
            expected_status=expected_status,
            new_status=outcome,
            expected_epoch=expected_epoch,
            event_type="TRANSITION",
            reason=f"{reason_prefix} {outcome.value}",
        )

    if retries_remaining > 0:
        next_attempt = datetime.now(tz=UTC) + compute_backoff(expected_epoch, backoff_base_seconds)
        return await state.transition(
            conn,
            call_id=call_id,
            expected_status=expected_status,
            new_status=CallStatus.RETRY_PENDING,
            expected_epoch=expected_epoch,
            event_type="TRANSITION",
            reason=f"{reason_prefix} {outcome.value}; backoff scheduled",
            column_updates={
                "next_attempt_at": next_attempt,
                "retries_remaining": retries_remaining - 1,
            },
        )

    return await state.transition(
        conn,
        call_id=call_id,
        expected_status=expected_status,
        new_status=CallStatus.FAILED,
        expected_epoch=expected_epoch,
        event_type="TRANSITION",
        reason=f"{reason_prefix} {outcome.value}; retries exhausted",
    )
