from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.state.types import CallStatus


@dataclass(frozen=True, slots=True)
class CallHandle:
    provider_call_id: str
    accepted_at: datetime


@dataclass(frozen=True, slots=True)
class ProviderEvent:
    # Parsed out of a provider webhook payload. Module-level parse_event in
    # each adapter is responsible for mapping vendor-specific shapes into this
    # closed type.
    provider_event_id: str
    provider_call_id: str
    status_enum: CallStatus


class ProviderRejected(Exception):
    # Expected, domain-meaningful rejection (invalid number, blocked, etc.).
    # Scheduler catches this and transitions the call to FAILED — no retry,
    # since the reason won't change on re-dial.

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class ProviderUnavailable(Exception):
    # Infrastructure failure (5xx, timeout, connection reset). Scheduler
    # catches this and schedules a retry with backoff if budget remains.
    pass
