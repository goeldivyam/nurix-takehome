from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

# P1D owns `app.audit.emitter`. Same rationale as machine.py: if the module
# hasn't landed yet, ImportError surfaces immediately rather than silently
# dropping CAMPAIGN_PROMOTED_ACTIVE / CAMPAIGN_COMPLETED audit rows.
from app.audit.emitter import emit_audit
from app.audit.events import AuditEvent

if TYPE_CHECKING:
    import asyncpg


async def maybe_promote_to_active(conn: asyncpg.Connection, campaign_id: UUID) -> None:
    # Called whenever a call in this campaign first moves into DIALING. CAS on
    # status='PENDING' so concurrent claim attempts across different calls in
    # the same newly-activated campaign only emit ONE promotion audit row.
    row = await conn.fetchrow(
        """
        UPDATE campaigns
        SET status = 'ACTIVE', updated_at = NOW()
        WHERE id = $1 AND status = 'PENDING'
        RETURNING id
        """,
        campaign_id,
    )
    if row is None:
        return

    await emit_audit(
        conn,
        AuditEvent(
            event_type="CAMPAIGN_PROMOTED_ACTIVE",
            reason="first call entered DIALING",
            campaign_id=campaign_id,
            state_before="PENDING",
            state_after="ACTIVE",
        ),
    )


async def maybe_transition_campaign_terminal(conn: asyncpg.Connection, campaign_id: UUID) -> None:
    # Called from state.transition() after every terminal call transition.
    # If the campaign has no more in-flight / queued / retry-pending work,
    # fold it to its own terminal state. CAS on status='ACTIVE' naturally
    # serializes the race when the last two calls terminate simultaneously:
    # whichever connection commits first wins; the second sees a no-op.
    active = await conn.fetchval(
        """
        SELECT COUNT(*) FROM calls
        WHERE campaign_id = $1
          AND status IN ('QUEUED', 'DIALING', 'IN_PROGRESS', 'RETRY_PENDING')
        """,
        campaign_id,
    )
    if active is not None and active > 0:
        return

    # Aggregate terminal call dispositions so the audit row carries a tidy
    # "why" snapshot — operators shouldn't need a second query to see what
    # the campaign looked like at rollup time.
    counts_row = await conn.fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE status = 'COMPLETED')  AS completed,
            COUNT(*) FILTER (WHERE status = 'FAILED')     AS failed,
            COUNT(*) FILTER (WHERE status = 'NO_ANSWER')  AS no_answer,
            COUNT(*) FILTER (WHERE status = 'BUSY')       AS busy
        FROM calls
        WHERE campaign_id = $1
        """,
        campaign_id,
    )
    completed = int(counts_row["completed"]) if counts_row else 0
    failed = int(counts_row["failed"]) if counts_row else 0
    no_answer = int(counts_row["no_answer"]) if counts_row else 0
    busy = int(counts_row["busy"]) if counts_row else 0

    target = "COMPLETED" if completed > 0 else "FAILED"

    updated = await conn.fetchrow(
        """
        UPDATE campaigns
        SET status = $2, updated_at = NOW()
        WHERE id = $1 AND status = 'ACTIVE'
        RETURNING id
        """,
        campaign_id,
        target,
    )
    if updated is None:
        # Lost the CAS race — a sibling terminal transition already folded
        # the campaign. Their audit row is the canonical one.
        return

    await emit_audit(
        conn,
        AuditEvent(
            event_type="CAMPAIGN_COMPLETED",
            reason=f"all calls terminal; rolling up to {target}",
            campaign_id=campaign_id,
            state_before="ACTIVE",
            state_after=target,
            extra={
                "terminal": target,
                "completed": completed,
                "failed": failed,
                "no_answer": no_answer,
                "busy": busy,
            },
        ),
    )
