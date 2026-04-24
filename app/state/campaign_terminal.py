from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

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
    # fold it to its own terminal state.
    #
    # `SELECT ... FOR NO KEY UPDATE` serializes concurrent rollup attempts
    # on the same campaign row. Under READ COMMITTED, two concurrent
    # terminal transactions on the last two calls could otherwise each read
    # the OTHER call as still active (each other's UPDATE not yet committed),
    # both skip the rollup, and the campaign is stranded ACTIVE with zero
    # active calls. The status='ACTIVE' CAS on the final UPDATE only guards
    # against DOUBLE rollup, not against ZERO rollup. Taking the row-level
    # lock here forces the second transaction to wait, so when its SELECT
    # COUNT runs it sees the first txn's committed calls UPDATE and proceeds
    # with the rollup itself.
    #
    # `FOR NO KEY UPDATE` (not plain `FOR UPDATE`) is the right variant:
    # the CAS below updates only non-key columns (status, updated_at), and
    # the weaker lock doesn't block concurrent INSERTs on FK-referencing
    # tables (calls, scheduler_audit) — important because the caller's
    # transaction is simultaneously inserting an audit row. Lock ordering
    # from the caller's side is always `calls` row (already UPDATEd by the
    # terminal transition) then `campaigns` row here — consistent across
    # all callers, so no deadlock opportunity.
    await conn.execute("SELECT 1 FROM campaigns WHERE id = $1 FOR NO KEY UPDATE", campaign_id)
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
