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
# The non-terminal and in-flight sets were once declared here for symmetry;
# every call site elected to spell the SQL literal list inline for query
# readability, so the constants were dead weight. Keep only the terminal set
# (imported by `state.machine` to fire the rollup hook).
