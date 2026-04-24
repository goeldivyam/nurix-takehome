from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

# Closed set of audit event types the system emits. Every entry below has a
# concrete emit site in production code — we don't carry declarative placeholders
# that render as empty filter rows in the UI.
EventType = Literal[
    "CLAIMED",
    "DISPATCH",
    "RETRY_DUE",
    "SKIP_BUSINESS_HOUR",
    "SKIP_CONCURRENCY",
    "WEBHOOK_IGNORED_STALE",
    "TRANSITION",
    "RECLAIM_SKIPPED_TERMINAL",
    "RECLAIM_EXECUTED",
    "CAMPAIGN_PROMOTED_ACTIVE",
    "CAMPAIGN_COMPLETED",
    "DEBUG_AGE_DIALING",
]


@dataclass(frozen=True, slots=True)
class AuditEvent:
    # Pure value object. No .save() / .emit() — I/O goes through
    # audit.emitter.emit_audit(conn, event) on the caller's connection so the
    # write joins the triggering transaction.
    #
    # `phone` and `attempt_epoch` are denormalized emit-time snapshots of the
    # call-scoped context. Populated ONLY on call-scoped events; left None on
    # campaign-level events (SKIP_*/CAMPAIGN_*). See schema.sql comment on
    # scheduler_audit for the immutability rule that governs what may be
    # denormalized here.
    event_type: EventType
    reason: str
    campaign_id: UUID | None = None
    call_id: UUID | None = None
    phone: str | None = None
    attempt_epoch: int | None = None
    state_before: str | None = None
    state_after: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
