from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING
from uuid import UUID

from app.persistence.repositories import CallRepo, CallRow
from app.state import machine as state
from app.state.types import TERMINAL_CALL_STATUSES, CallStatus

if TYPE_CHECKING:
    from app.deps import Deps

logger = logging.getLogger(__name__)


class ReclaimKind(str, Enum):
    # Classification of a single stuck-row outcome. `EXECUTED` is the hot path
    # when the provider is unresponsive; `TERMINAL_APPLIED` folds a confirmed
    # provider disposition without bumping the epoch; `SKIPPED_NO_OP` reflects
    # a CAS miss (a webhook moved the row between the find and the update);
    # `FAILED` captures an exception caught INSIDE the per-row task so its
    # sibling TaskGroup peers are never cancelled.
    TERMINAL_APPLIED = "TERMINAL_APPLIED"
    EXECUTED = "EXECUTED"
    SKIPPED_NO_OP = "SKIPPED_NO_OP"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ReclaimOutcome:
    call_id: UUID
    kind: ReclaimKind
    detail: str | None = None


async def stuck_reclaim_sweep(deps: Deps) -> list[ReclaimOutcome]:
    # One sweep invocation. Returns the per-row outcomes so tests and the
    # long-running daemon can observe progress without re-reading the DB.
    # Wake is notified only when a reclaim or terminal-apply actually freed
    # capacity — stale no-ops aren't worth poking the scheduler for.
    settings = deps.settings
    async with deps.pools.scheduler.acquire() as conn:
        rows = await CallRepo.find_stuck_dialing(conn, settings.stuck_reclaim_seconds)
    if not rows:
        return []

    # Per-row tasks each catch their own exceptions (see `_reclaim_one`).
    # The TaskGroup here is a fan-out primitive, not an error-propagation one.
    async with asyncio.TaskGroup() as tg:
        tasks = [tg.create_task(_reclaim_one(deps, row)) for row in rows]

    outcomes = [t.result() for t in tasks]

    if any(o.kind in {ReclaimKind.EXECUTED, ReclaimKind.TERMINAL_APPLIED} for o in outcomes):
        deps.wake.notify()
    return outcomes


async def _reclaim_one(deps: Deps, row: CallRow) -> ReclaimOutcome:
    # Outer `except BaseException` is intentional: a TaskGroup cancels every
    # sibling task on the first escaping exception, which would lose their
    # in-flight Results. Turning every failure into a typed outcome keeps
    # the sweep resilient to a single slow / crashing get_status call.
    try:
        return await _reclaim_one_inner(deps, row)
    except BaseException as exc:
        logger.exception("reclaim for %s failed", row.id)
        return ReclaimOutcome(
            call_id=row.id,
            kind=ReclaimKind.FAILED,
            detail=f"{type(exc).__name__}: {exc}",
        )


async def _reclaim_one_inner(deps: Deps, row: CallRow) -> ReclaimOutcome:
    terminal_status: CallStatus | None = None

    # Null-handle short-circuit: `place_call` never returned a handle (Phase 2
    # crashed between the claim and the handle write). No real dial happened,
    # so skip the confirm and take the reclaim branch directly. The reclaim
    # bump + the subsequent claim's bump mean any retroactive event for the
    # original epoch CAS-no-ops at both the reclaimed epoch and the redialed
    # epoch — no stale event can ever be accepted.
    if row.provider_call_id is not None:
        reported = await _best_effort_get_status(deps, row.provider_call_id)
        if reported is not None and reported in TERMINAL_CALL_STATUSES:
            terminal_status = reported

    if terminal_status is not None:
        return await _apply_terminal(deps, row, terminal_status)
    return await _apply_reclaim(deps, row)


async def _best_effort_get_status(deps: Deps, provider_call_id: str) -> CallStatus | None:
    # Bounded by `stuck_reclaim_get_status_timeout_seconds` (strictly less than
    # the sweep interval) so a provider brown-out never head-of-line-blocks
    # the sweep. Any error path returns None and the caller treats it as
    # "unknown" — the reclaim CAS is the correctness backstop.
    try:
        return await asyncio.wait_for(
            deps.provider.get_status(provider_call_id),
            timeout=deps.settings.stuck_reclaim_get_status_timeout_seconds,
        )
    except TimeoutError:
        logger.warning("reclaim get_status timed out for %s", provider_call_id)
        return None
    except Exception:
        logger.exception("reclaim get_status failed for %s", provider_call_id)
        return None


async def _apply_terminal(
    deps: Deps,
    row: CallRow,
    terminal_status: CallStatus,
) -> ReclaimOutcome:
    # Same attempt_epoch — no bump. If a webhook arrived mid-sweep and already
    # moved the row past DIALING, the CAS no-ops; that's the correct outcome.
    async with deps.pools.scheduler.acquire() as conn, conn.transaction():
        result = await state.transition(
            conn,
            call_id=row.id,
            expected_status=CallStatus.DIALING,
            new_status=terminal_status,
            expected_epoch=row.attempt_epoch,
            event_type="RECLAIM_SKIPPED_TERMINAL",
            reason=f"provider confirmed {terminal_status.value}",
            extra={"provider_call_id": row.provider_call_id},
        )
    if result.is_no_op():
        return ReclaimOutcome(
            call_id=row.id,
            kind=ReclaimKind.SKIPPED_NO_OP,
            detail=terminal_status.value,
        )
    return ReclaimOutcome(
        call_id=row.id,
        kind=ReclaimKind.TERMINAL_APPLIED,
        detail=terminal_status.value,
    )


async def _apply_reclaim(deps: Deps, row: CallRow) -> ReclaimOutcome:
    # Unknown provider state (null handle, timeout, or non-terminal report).
    # Bump the epoch and return the row to QUEUED so a subsequent claim
    # produces a fresh idempotency key at the provider boundary.
    async with deps.pools.scheduler.acquire() as conn, conn.transaction():
        result = await state.transition(
            conn,
            call_id=row.id,
            expected_status=CallStatus.DIALING,
            new_status=CallStatus.QUEUED,
            expected_epoch=row.attempt_epoch,
            new_epoch=row.attempt_epoch + 1,
            event_type="RECLAIM_EXECUTED",
            reason="DIALING exceeded grace window; bumping epoch",
            extra={
                "previous_epoch": row.attempt_epoch,
                "provider_call_id": row.provider_call_id,
            },
        )
    if result.is_no_op():
        return ReclaimOutcome(call_id=row.id, kind=ReclaimKind.SKIPPED_NO_OP)
    return ReclaimOutcome(call_id=row.id, kind=ReclaimKind.EXECUTED)


async def stuck_reclaim_sweep_loop(deps: Deps) -> None:
    # Long-running daemon. Demo mode shortens the interval to 5s so a live
    # demo surfaces RECLAIM_EXECUTED inside the window rather than after 30s
    # of dead air. Errors never crash the loop — we log and retry.
    interval = deps.settings.reclaim_sweep_interval_effective
    while True:
        try:
            await stuck_reclaim_sweep(deps)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("stuck_reclaim_sweep crashed; will retry after interval")
        await asyncio.sleep(interval)
