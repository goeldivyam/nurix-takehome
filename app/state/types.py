from __future__ import annotations

from enum import Enum


class CallStatus(str, Enum):
    # Values match the DB CHECK constraint on calls.status verbatim so repos
    # can round-trip without translation.
    QUEUED = "QUEUED"
    DIALING = "DIALING"
    IN_PROGRESS = "IN_PROGRESS"
    RETRY_PENDING = "RETRY_PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    NO_ANSWER = "NO_ANSWER"
    BUSY = "BUSY"


class CampaignStatus(str, Enum):
    # Values match the DB CHECK constraint on campaigns.status verbatim.
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


TERMINAL_CALL_STATUSES: frozenset[CallStatus] = frozenset(
    {CallStatus.COMPLETED, CallStatus.FAILED, CallStatus.NO_ANSWER, CallStatus.BUSY}
)

NON_TERMINAL_CALL_STATUSES: frozenset[CallStatus] = frozenset(
    {
        CallStatus.QUEUED,
        CallStatus.DIALING,
        CallStatus.IN_PROGRESS,
        CallStatus.RETRY_PENDING,
    }
)

IN_FLIGHT_CALL_STATUSES: frozenset[CallStatus] = frozenset(
    {CallStatus.DIALING, CallStatus.IN_PROGRESS}
)
