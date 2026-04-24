from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from app.audit.emitter import emit_audit
from app.audit.events import AuditEvent, EventType
from app.state.campaign_terminal import maybe_transition_campaign_terminal
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
    # for the freshly-updated row; None on no-op. `rejected_reason` is set
    # when the transition was refused at the invariant-guard layer BEFORE
    # hitting the DB — distinct from a stale CAS so callers can emit a more
    # specific audit row (e.g. "terminal-wins" vs "CAS no-op").
    applied: bool
    row: dict[str, Any] | None
    rejected_reason: str | None = None

    def is_no_op(self) -> bool:
        return not self.applied

    def is_terminal_regression(self) -> bool:
        return self.rejected_reason == "terminal_regression"

    @classmethod
    def no_op(cls) -> TransitionResult:
        return cls(applied=False, row=None)

    @classmethod
    def terminal_regression(cls) -> TransitionResult:
        return cls(applied=False, row=None, rejected_reason="terminal_regression")

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

    # Terminal-is-absorbing invariant (the state machine is the sole mutator,
    # so the invariant belongs here — not in any one caller). Once a call
    # reaches a terminal status, no transition may move it anywhere else —
    # not to a non-terminal (e.g. late IN_PROGRESS after COMPLETED) and not
    # to a DIFFERENT terminal (e.g. contradictory FAILED webhook arriving
    # after a legitimate COMPLETED landed). Both edges are race artefacts of
    # at-least-once + out-of-order webhook delivery; terminal always wins
    # the first time it's applied, and the audit log preserves the losing
    # event forensically via a distinct `terminal-wins` reason.
    #
    # Reject BEFORE the DB write so callers get a distinct `rejected_reason`
    # they can audit differently from a plain stale CAS. The webhook
    # processor relies on this; internal callers (scheduler tick, reclaim,
    # retry_apply) never construct such an edge by design, so this guard is
    # defense-in-depth for them. `expected == new` (a trivial same-state
    # update) is allowed through to the CAS because the same-state case is
    # already filtered by the webhook adapter and never arises internally.
    _terminal_values = {s.value for s in TERMINAL_CALL_STATUSES}
    if expected_status_str in _terminal_values and new_status_str != expected_status_str:
        return TransitionResult.terminal_regression()

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

    # `phone` and `attempt_epoch` are denormalized emit-time snapshots on
    # the audit row. The CAS UPDATE above used `RETURNING *` so row_dict
    # already carries both — no extra query on the hot path. This one site
    # covers every audit event emitted via state.transition (CLAIMED from
    # reclaim path, DISPATCH, TRANSITION, RECLAIM_EXECUTED,
    # RECLAIM_SKIPPED_TERMINAL). Emitters outside state.transition (tick's
    # own CLAIMED, webhook_processor WEBHOOK_IGNORED_STALE, debug) populate
    # these fields themselves from the call row already in scope.
    await emit_audit(
        conn,
        AuditEvent(
            event_type=event_type,
            reason=reason,
            campaign_id=row_dict["campaign_id"],
            call_id=call_id,
            phone=row_dict.get("phone"),
            attempt_epoch=row_dict.get("attempt_epoch"),
            state_before=expected_status_str,
            state_after=new_status_str,
            extra=extra or {},
        ),
    )

    # Campaign-level rollup side-effect. PENDING → ACTIVE promotion is NOT
    # driven from here: the sole QUEUED→DIALING path runs through
    # `CallRepo.claim_next_queued` (a raw UPDATE, not `state.transition`), so
    # the scheduler's Phase 1 explicitly calls `maybe_promote_to_active` on
    # the same txn. Adding a promote hook here would be dead code today.
    if new_status_str in {s.value for s in TERMINAL_CALL_STATUSES}:
        await maybe_transition_campaign_terminal(conn, row_dict["campaign_id"])

    return TransitionResult.applied_(row_dict)
