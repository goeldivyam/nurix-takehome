from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

# P1D owns `app.audit.emitter` and provides
# `async def emit_audit(conn, event: AuditEvent) -> None`. If P1D hasn't
# landed yet, importing this module will raise ImportError — that's intended:
# the state machine must NEVER silently swallow a missing audit writer, since
# the audit log IS the visualization per rubric #7.
from app.audit.emitter import emit_audit
from app.audit.events import AuditEvent, EventType
from app.state.campaign_terminal import (
    maybe_promote_to_active,
    maybe_transition_campaign_terminal,
)
from app.state.types import TERMINAL_CALL_STATUSES, CallStatus

if TYPE_CHECKING:
    import asyncpg


# Closed allow-list of non-status columns the state machine is authorized to
# write. Anything else must go through a dedicated primitive — keeps this
# module the single mutator of every call-row field that matters.
_ALLOWED_COLUMN_UPDATES: frozenset[str] = frozenset(
    {"provider_call_id", "next_attempt_at", "retries_remaining"}
)


@dataclass(frozen=True, slots=True)
class TransitionResult:
    # Return type for state.transition(). `applied=False` means the CAS
    # UPDATE matched zero rows (stale expected_status / expected_epoch) —
    # caller handles idempotently. `row` is a dict copy of the asyncpg Record
    # for the freshly-updated row; None on no-op.
    applied: bool
    row: dict[str, Any] | None

    def is_no_op(self) -> bool:
        return not self.applied

    @classmethod
    def no_op(cls) -> TransitionResult:
        return cls(applied=False, row=None)

    @classmethod
    def applied_(cls, row: dict[str, Any]) -> TransitionResult:
        return cls(applied=True, row=row)


def _status_value(status: CallStatus | str) -> str:
    return status.value if isinstance(status, CallStatus) else status


async def transition(
    conn: asyncpg.Connection,
    *,
    call_id: UUID,
    expected_status: CallStatus | str,
    new_status: CallStatus | str,
    expected_epoch: int,
    new_epoch: int | None = None,
    event_type: EventType,
    reason: str,
    extra: dict[str, Any] | None = None,
    column_updates: dict[str, Any] | None = None,
) -> TransitionResult:
    # The ONE function that mutates a call row. Every caller (scheduler,
    # webhook processor, reclaim sweep) MUST go through here so every mutation
    # carries a CAS guard and a same-transaction audit row.
    #
    # All database work below runs on the caller's connection — the transition
    # + its audit row + any campaign-level side-effects share one transaction
    # so a rollback upstream discards the whole set atomically.
    if column_updates:
        unauthorized = set(column_updates) - _ALLOWED_COLUMN_UPDATES
        if unauthorized:
            # Fail loud BEFORE touching the DB. Sorted for deterministic
            # error messages (tests + logs).
            key = sorted(unauthorized)[0]
            raise ValueError(f"unauthorized column update: {key}")

    expected_status_str = _status_value(expected_status)
    new_status_str = _status_value(new_status)

    # Build the SET clause. `attempt_epoch` policy:
    #   - new_epoch is None  -> preserve current epoch (most transitions).
    #   - new_epoch is int   -> set to that explicit value (claim bumps to
    #                           expected_epoch + 1; reclaim bumps similarly).
    set_parts: list[str] = ["status = $1", "updated_at = NOW()"]
    # $1 new_status, $2 call_id, $3 expected_status, $4 expected_epoch
    params: list[Any] = [new_status_str, call_id, expected_status_str, expected_epoch]

    if new_epoch is not None:
        params.append(new_epoch)
        set_parts.append(f"attempt_epoch = ${len(params)}")

    if column_updates:
        # Deterministic ordering keeps parameter positions stable across calls,
        # which makes query plans cacheable on the asyncpg side.
        for col in sorted(column_updates):
            params.append(column_updates[col])
            set_parts.append(f"{col} = ${len(params)}")

    # `set_parts` is built from validated column names (status + the closed
    # allow-list checked above); values travel via $N placeholders.
    set_clause = ",\n            ".join(set_parts)
    sql = f"""
        UPDATE calls
        SET {set_clause}
        WHERE id = $2 AND status = $3 AND attempt_epoch = $4
        RETURNING *
    """  # noqa: S608

    row = await conn.fetchrow(sql, *params)
    if row is None:
        # CAS mismatched (stale status or epoch). Expected on webhook
        # reordering / races; emit no audit row here because the caller
        # (e.g. webhook processor) is responsible for emitting WEBHOOK_IGNORED
        # with full context.
        return TransitionResult.no_op()

    row_dict: dict[str, Any] = dict(row)

    await emit_audit(
        conn,
        AuditEvent(
            event_type=event_type,
            reason=reason,
            campaign_id=row_dict["campaign_id"],
            call_id=call_id,
            state_before=expected_status_str,
            state_after=new_status_str,
            extra=extra or {},
        ),
    )

    # Campaign-level side-effects. Only fire when the status actually moves —
    # a same-status column update (e.g. DIALING->DIALING writing
    # provider_call_id) should not re-trigger promotion.
    if new_status_str != expected_status_str and new_status_str == CallStatus.DIALING.value:
        await maybe_promote_to_active(conn, row_dict["campaign_id"])

    if new_status_str in {s.value for s in TERMINAL_CALL_STATUSES}:
        await maybe_transition_campaign_terminal(conn, row_dict["campaign_id"])

    return TransitionResult.applied_(row_dict)
