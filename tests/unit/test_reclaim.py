from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

# P1D's audit emitter must resolve at import time because the state machine
# imports `emit_audit` at module top. Integration wiring lands the real one;
# this stub keeps the unit tests hermetic.
if "app.audit.emitter" not in sys.modules:
    _stub = ModuleType("app.audit.emitter")

    async def _emit_audit_stub(conn: Any, event: Any) -> None:
        return None

    _stub.emit_audit = _emit_audit_stub  # type: ignore[attr-defined]
    sys.modules["app.audit.emitter"] = _stub

from app.persistence.repositories import CallRow
from app.scheduler import reclaim as reclaim_module
from app.scheduler.reclaim import (
    ReclaimKind,
    ReclaimOutcome,
    _reclaim_one,
    _reclaim_one_inner,
    stuck_reclaim_sweep,
)
from app.state.types import CallStatus


def _call_row(
    *,
    provider_call_id: str | None = None,
    attempt_epoch: int = 1,
    status: str = "DIALING",
) -> CallRow:
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    return CallRow(
        id=uuid4(),
        campaign_id=uuid4(),
        phone="+14155550001",
        status=status,
        attempt_epoch=attempt_epoch,
        retries_remaining=2,
        next_attempt_at=None,
        provider_call_id=provider_call_id,
        created_at=now,
        updated_at=now,
    )


class _FakeTransactionContext:
    async def __aenter__(self) -> _FakeTransactionContext:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeConn:
    # Stand-in for an asyncpg connection. Tests that hit the DB via
    # state.transition are patched to intercept at the `state.transition` call
    # site, so this object just needs to support `conn.transaction()`.
    def transaction(self) -> _FakeTransactionContext:
        return _FakeTransactionContext()


class _FakePool:
    def __init__(self, conn: _FakeConn | None = None) -> None:
        self._conn = conn or _FakeConn()

    def acquire(self) -> _FakeAcquireContext:
        return _FakeAcquireContext(self._conn)


class _FakeAcquireContext:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


def _make_deps(
    *,
    provider: Any,
    settings_overrides: dict[str, Any] | None = None,
) -> Any:
    # SimpleNamespace keeps the unit tests hermetic — we pass only the
    # attributes reclaim reads. The real `Deps` dataclass isn't instantiated
    # so we don't need a Settings() with an env file etc.
    settings = SimpleNamespace(
        stuck_reclaim_seconds=60,
        stuck_reclaim_get_status_timeout_seconds=1.0,
        reclaim_sweep_interval_effective=30.0,
    )
    if settings_overrides:
        for k, v in settings_overrides.items():
            setattr(settings, k, v)
    pools = SimpleNamespace(scheduler=_FakePool())
    wake = SimpleNamespace(notify=MagicMock())
    return SimpleNamespace(
        settings=settings,
        pools=pools,
        provider=provider,
        wake=wake,
    )


class TestReclaimOneInner:
    async def test_null_handle_skips_get_status_and_reclaims(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # provider.get_status must never be called for a null-handle row.
        # The reclaim branch runs the CAS with a bumped epoch.
        transition_mock = AsyncMock()
        transition_mock.return_value = SimpleNamespace(is_no_op=lambda: False)
        monkeypatch.setattr(reclaim_module.state, "transition", transition_mock)

        provider = SimpleNamespace(get_status=AsyncMock())
        deps = _make_deps(provider=provider)
        row = _call_row(provider_call_id=None, attempt_epoch=3)

        outcome = await _reclaim_one_inner(deps, row)

        provider.get_status.assert_not_awaited()
        transition_mock.assert_awaited_once()
        kwargs = transition_mock.await_args.kwargs
        assert kwargs["expected_status"] is CallStatus.DIALING
        assert kwargs["new_status"] is CallStatus.QUEUED
        assert kwargs["expected_epoch"] == 3
        assert kwargs["new_epoch"] == 4
        assert kwargs["event_type"] == "RECLAIM_EXECUTED"
        # `provider_call_id=None` is load-bearing: without it, a late webhook
        # from the dead attempt could resolve back to this row via
        # `CallRepo.get_by_provider_call_id` and race-CAS the new epoch.
        assert kwargs["column_updates"] == {"provider_call_id": None}
        assert outcome.kind is ReclaimKind.EXECUTED

    async def test_provider_terminal_applies_same_epoch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        transition_mock = AsyncMock()
        transition_mock.return_value = SimpleNamespace(is_no_op=lambda: False)
        monkeypatch.setattr(reclaim_module.state, "transition", transition_mock)

        provider = SimpleNamespace(
            get_status=AsyncMock(return_value=CallStatus.COMPLETED),
        )
        deps = _make_deps(provider=provider)
        row = _call_row(provider_call_id="pc-1", attempt_epoch=2)

        outcome = await _reclaim_one_inner(deps, row)

        provider.get_status.assert_awaited_once_with("pc-1")
        transition_mock.assert_awaited_once()
        kwargs = transition_mock.await_args.kwargs
        assert kwargs["new_status"] is CallStatus.COMPLETED
        assert kwargs["expected_epoch"] == 2
        # Terminal-apply path MUST NOT pass new_epoch.
        assert "new_epoch" not in kwargs or kwargs.get("new_epoch") is None
        assert kwargs["event_type"] == "RECLAIM_SKIPPED_TERMINAL"
        assert outcome.kind is ReclaimKind.TERMINAL_APPLIED
        assert outcome.detail == "COMPLETED"

    async def test_get_status_timeout_falls_through_to_reclaim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        transition_mock = AsyncMock()
        transition_mock.return_value = SimpleNamespace(is_no_op=lambda: False)
        monkeypatch.setattr(reclaim_module.state, "transition", transition_mock)

        # asyncio.wait_for raises TimeoutError on slow futures — simulate by
        # making get_status hang long enough to trip wait_for's timeout. We
        # use a short sleep rather than asyncio.Future() so the test stays
        # sub-second even under a stuck scheduler.
        async def slow_get_status(_pc: str) -> CallStatus:
            await asyncio.sleep(5)
            return CallStatus.COMPLETED

        provider = SimpleNamespace(get_status=slow_get_status)
        deps = _make_deps(
            provider=provider,
            settings_overrides={"stuck_reclaim_get_status_timeout_seconds": 0.01},
        )
        row = _call_row(provider_call_id="pc-2", attempt_epoch=1)

        outcome = await _reclaim_one_inner(deps, row)

        transition_mock.assert_awaited_once()
        kwargs = transition_mock.await_args.kwargs
        assert kwargs["new_status"] is CallStatus.QUEUED
        assert kwargs["new_epoch"] == 2
        assert kwargs["event_type"] == "RECLAIM_EXECUTED"
        assert kwargs["column_updates"] == {"provider_call_id": None}
        assert outcome.kind is ReclaimKind.EXECUTED

    async def test_provider_get_status_raises_falls_through_to_reclaim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        transition_mock = AsyncMock()
        transition_mock.return_value = SimpleNamespace(is_no_op=lambda: False)
        monkeypatch.setattr(reclaim_module.state, "transition", transition_mock)

        provider = SimpleNamespace(
            get_status=AsyncMock(side_effect=RuntimeError("provider down")),
        )
        deps = _make_deps(provider=provider)
        row = _call_row(provider_call_id="pc-3", attempt_epoch=1)

        outcome = await _reclaim_one_inner(deps, row)

        # Error from the provider is swallowed at the best-effort boundary
        # and the reclaim branch runs exactly like the unknown-status case.
        assert outcome.kind is ReclaimKind.EXECUTED

    async def test_terminal_branch_cas_no_op_reports_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        transition_mock = AsyncMock()
        transition_mock.return_value = SimpleNamespace(is_no_op=lambda: True)
        monkeypatch.setattr(reclaim_module.state, "transition", transition_mock)

        provider = SimpleNamespace(
            get_status=AsyncMock(return_value=CallStatus.FAILED),
        )
        deps = _make_deps(provider=provider)
        row = _call_row(provider_call_id="pc-4", attempt_epoch=1)

        outcome = await _reclaim_one_inner(deps, row)
        assert outcome.kind is ReclaimKind.SKIPPED_NO_OP
        assert outcome.detail == "FAILED"

    async def test_non_terminal_provider_status_reclaims(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Provider still reports DIALING / IN_PROGRESS — treat as unknown
        # and reclaim. The grace window + CAS protects against a confused
        # provider cache silently re-activating a dead call.
        transition_mock = AsyncMock()
        transition_mock.return_value = SimpleNamespace(is_no_op=lambda: False)
        monkeypatch.setattr(reclaim_module.state, "transition", transition_mock)

        provider = SimpleNamespace(
            get_status=AsyncMock(return_value=CallStatus.IN_PROGRESS),
        )
        deps = _make_deps(provider=provider)
        row = _call_row(provider_call_id="pc-5", attempt_epoch=1)

        outcome = await _reclaim_one_inner(deps, row)

        transition_mock.assert_awaited_once()
        kwargs = transition_mock.await_args.kwargs
        assert kwargs["new_status"] is CallStatus.QUEUED
        assert kwargs["event_type"] == "RECLAIM_EXECUTED"
        assert outcome.kind is ReclaimKind.EXECUTED


class TestReclaimOneWrapper:
    async def test_base_exception_is_captured_not_reraised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def raising_inner(_deps: Any, _row: CallRow) -> ReclaimOutcome:
            # BaseException — NOT Exception — to prove the outer guard is
            # wide enough to prevent a TaskGroup cancellation cascade.
            raise KeyboardInterrupt("simulated")

        monkeypatch.setattr(reclaim_module, "_reclaim_one_inner", raising_inner)

        deps = _make_deps(provider=SimpleNamespace(get_status=AsyncMock()))
        row = _call_row(provider_call_id=None)

        outcome = await _reclaim_one(deps, row)
        assert outcome.kind is ReclaimKind.FAILED
        assert outcome.detail is not None
        assert outcome.detail.startswith("KeyboardInterrupt:")
        assert outcome.call_id == row.id

    async def test_regular_exception_is_captured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def raising_inner(_deps: Any, _row: CallRow) -> ReclaimOutcome:
            raise ValueError("boom")

        monkeypatch.setattr(reclaim_module, "_reclaim_one_inner", raising_inner)

        deps = _make_deps(provider=SimpleNamespace(get_status=AsyncMock()))
        row = _call_row(provider_call_id=None)

        outcome = await _reclaim_one(deps, row)
        assert outcome.kind is ReclaimKind.FAILED
        assert outcome.detail is not None
        assert outcome.detail.startswith("ValueError:")


class TestStuckReclaimSweep:
    async def test_empty_rows_skips_provider_and_transition(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        find_mock = AsyncMock(return_value=[])
        monkeypatch.setattr(reclaim_module.CallRepo, "find_stuck_dialing", find_mock)

        transition_mock = AsyncMock()
        monkeypatch.setattr(reclaim_module.state, "transition", transition_mock)

        provider = SimpleNamespace(get_status=AsyncMock())
        deps = _make_deps(provider=provider)

        outcomes = await stuck_reclaim_sweep(deps)

        assert outcomes == []
        provider.get_status.assert_not_awaited()
        transition_mock.assert_not_awaited()
        # No actionable outcome means no wake notify.
        deps.wake.notify.assert_not_called()

    async def test_sibling_isolation_one_row_raises_other_completes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        good_row = _call_row(provider_call_id=None, attempt_epoch=1)
        bad_row = _call_row(provider_call_id=None, attempt_epoch=1)

        monkeypatch.setattr(
            reclaim_module.CallRepo,
            "find_stuck_dialing",
            AsyncMock(return_value=[good_row, bad_row]),
        )

        # Patch `_reclaim_one_inner` so one call succeeds and the other
        # raises a BaseException — the wrapper must turn both into results.
        async def inner(_deps: Any, row: CallRow) -> ReclaimOutcome:
            if row.id == bad_row.id:
                raise SystemExit("bad row")
            return ReclaimOutcome(call_id=row.id, kind=ReclaimKind.EXECUTED)

        monkeypatch.setattr(reclaim_module, "_reclaim_one_inner", inner)

        deps = _make_deps(provider=SimpleNamespace(get_status=AsyncMock()))
        outcomes = await stuck_reclaim_sweep(deps)

        assert len(outcomes) == 2
        by_id = {o.call_id: o for o in outcomes}
        assert by_id[good_row.id].kind is ReclaimKind.EXECUTED
        assert by_id[bad_row.id].kind is ReclaimKind.FAILED
        assert by_id[bad_row.id].detail is not None
        assert by_id[bad_row.id].detail.startswith("SystemExit:")

    async def test_notify_fires_when_a_row_is_reclaimed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        row = _call_row(provider_call_id=None, attempt_epoch=1)
        monkeypatch.setattr(
            reclaim_module.CallRepo,
            "find_stuck_dialing",
            AsyncMock(return_value=[row]),
        )

        async def inner(_deps: Any, r: CallRow) -> ReclaimOutcome:
            return ReclaimOutcome(call_id=r.id, kind=ReclaimKind.EXECUTED)

        monkeypatch.setattr(reclaim_module, "_reclaim_one_inner", inner)

        deps = _make_deps(provider=SimpleNamespace(get_status=AsyncMock()))
        await stuck_reclaim_sweep(deps)

        deps.wake.notify.assert_called_once()

    async def test_no_notify_when_all_rows_were_no_op(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        row = _call_row(provider_call_id=None, attempt_epoch=1)
        monkeypatch.setattr(
            reclaim_module.CallRepo,
            "find_stuck_dialing",
            AsyncMock(return_value=[row]),
        )

        async def inner(_deps: Any, r: CallRow) -> ReclaimOutcome:
            # Stale no-op: a webhook beat us to the row. Scheduler doesn't
            # need a wake — the webhook processor already notified.
            return ReclaimOutcome(call_id=r.id, kind=ReclaimKind.SKIPPED_NO_OP)

        monkeypatch.setattr(reclaim_module, "_reclaim_one_inner", inner)

        deps = _make_deps(provider=SimpleNamespace(get_status=AsyncMock()))
        await stuck_reclaim_sweep(deps)

        deps.wake.notify.assert_not_called()
