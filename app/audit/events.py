from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

EventType = Literal[
    "CLAIMED",
    "DISPATCH",
    "RETRY_DUE",
    "SKIP_BUSINESS_HOUR",
    "SKIP_CONCURRENCY",
    "SKIP_RETRY_BACKOFF",
    "WEBHOOK_RECEIVED",
    "WEBHOOK_IGNORED_STALE",
    "TRANSITION",
    "RECLAIM_SKIPPED_TERMINAL",
    "RECLAIM_EXECUTED",
    "CAMPAIGN_PROMOTED_ACTIVE",
    "CAMPAIGN_COMPLETED",
    "DEBUG_AGE_DIALING",
]


EVENT_TYPES: tuple[EventType, ...] = (
    "CLAIMED",
    "DISPATCH",
    "RETRY_DUE",
    "SKIP_BUSINESS_HOUR",
    "SKIP_CONCURRENCY",
    "SKIP_RETRY_BACKOFF",
    "WEBHOOK_RECEIVED",
    "WEBHOOK_IGNORED_STALE",
    "TRANSITION",
    "RECLAIM_SKIPPED_TERMINAL",
    "RECLAIM_EXECUTED",
    "CAMPAIGN_PROMOTED_ACTIVE",
    "CAMPAIGN_COMPLETED",
    "DEBUG_AGE_DIALING",
)


@dataclass(frozen=True, slots=True)
class AuditEvent:
    # Pure value object. No .save() / .emit() — I/O goes through
    # audit.emitter.emit_audit(conn, event) on the caller's connection so the
    # write joins the triggering transaction.
    event_type: EventType
    reason: str
    campaign_id: UUID | None = None
    call_id: UUID | None = None
    state_before: str | None = None
    state_after: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
