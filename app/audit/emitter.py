from __future__ import annotations

import json
from typing import Any

import asyncpg

from app.audit.events import AuditEvent


async def emit_audit(conn: asyncpg.Connection[Any], event: AuditEvent) -> None:
    # Writes run on the CALLER's connection so the audit row joins the triggering
    # transaction — if the state transition rolls back, the audit row does too.
    # See `backend-conventions` skill: never use audit_pool for writes.
    await conn.execute(
        """
        INSERT INTO scheduler_audit
            (event_type, campaign_id, call_id, phone, attempt_epoch, reason,
             state_before, state_after, extra)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
        """,
        event.event_type,
        event.campaign_id,
        event.call_id,
        event.phone,
        event.attempt_epoch,
        event.reason,
        event.state_before,
        event.state_after,
        json.dumps(event.extra),
    )
