from __future__ import annotations

from enum import Enum

from app.provider.types import ProviderRejected, ProviderUnavailable
from app.state.types import CallStatus


class RetryOutcome(str, Enum):
    # The four classifier outputs the scheduler / state machine act on:
    #   TERMINAL            -> move call directly to FAILED (or COMPLETED, in the
    #                          success case) with no retry.
    #   TRANSIENT_RETRYABLE -> infra hiccup (provider 5xx, timeout); retry with
    #                          backoff if budget remains, else FAILED.
    #   NO_ANSWER / BUSY    -> telephony-level rejections that succeed on
    #                          re-dial; retry with backoff if budget remains.
    TERMINAL = "TERMINAL"
    TRANSIENT_RETRYABLE = "TRANSIENT_RETRYABLE"
    NO_ANSWER = "NO_ANSWER"
    BUSY = "BUSY"


# Inputs we classify. `CallStatus` covers the terminal-ish dispositions reported
# by the provider (COMPLETED / FAILED / NO_ANSWER / BUSY); the exception types
# cover pre-dispatch rejections from `place_call`.
ClassifierInput = CallStatus | ProviderRejected | ProviderUnavailable


def classify(outcome: ClassifierInput) -> RetryOutcome:
    if isinstance(outcome, ProviderRejected):
        # Rejection reason (invalid number, blocked, etc.) won't change on
        # re-dial — terminal by design.
        return RetryOutcome.TERMINAL
    if isinstance(outcome, ProviderUnavailable):
        return RetryOutcome.TRANSIENT_RETRYABLE
    if outcome is CallStatus.NO_ANSWER:
        return RetryOutcome.NO_ANSWER
    if outcome is CallStatus.BUSY:
        return RetryOutcome.BUSY
    if outcome is CallStatus.COMPLETED or outcome is CallStatus.FAILED:
        return RetryOutcome.TERMINAL
    # Non-terminal call statuses (QUEUED / DIALING / IN_PROGRESS /
    # RETRY_PENDING) never reach this classifier — the scheduler only asks
    # "what do we do with this outcome?" at terminal provider events. Surface
    # mis-use loudly rather than silently bucketing.
    raise ValueError(f"classify() got non-terminal outcome: {outcome!r}")
