from __future__ import annotations

import sys
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

# P1D (app.audit.emitter) may not have landed when this test module is first
# imported. Inject a stub into sys.modules BEFORE importing anything under
# app.state so the state machine's top-level `from app.audit.emitter import
# emit_audit` resolves cleanly. Tests that need to observe audit emission
# patch this same attribute per-test.
if "app.audit.emitter" not in sys.modules:
    _stub = ModuleType("app.audit.emitter")

    async def _emit_audit_stub(conn: Any, event: Any) -> None:
        return None

    _stub.emit_audit = _emit_audit_stub  # type: ignore[attr-defined]
    sys.modules["app.audit.emitter"] = _stub

from app.provider.types import ProviderRejected, ProviderUnavailable
from app.state import machine as state_machine
from app.state.machine import TransitionResult, transition
from app.state.retry_classification import RetryOutcome, classify
from app.state.types import CallStatus


class TestRetryClassification:
    def test_provider_rejected_is_terminal(self) -> None:
        assert classify(ProviderRejected("invalid_number")) is RetryOutcome.TERMINAL

    def test_provider_unavailable_is_transient_retryable(self) -> None:
        assert classify(ProviderUnavailable()) is RetryOutcome.TRANSIENT_RETRYABLE

    def test_no_answer(self) -> None:
        assert classify(CallStatus.NO_ANSWER) is RetryOutcome.NO_ANSWER

    def test_busy(self) -> None:
        assert classify(CallStatus.BUSY) is RetryOutcome.BUSY

    def test_completed_is_terminal(self) -> None:
        assert classify(CallStatus.COMPLETED) is RetryOutcome.TERMINAL

    def test_failed_is_terminal(self) -> None:
        assert classify(CallStatus.FAILED) is RetryOutcome.TERMINAL

    @pytest.mark.parametrize(
        "bad_status",
        [CallStatus.QUEUED, CallStatus.DIALING, CallStatus.IN_PROGRESS, CallStatus.RETRY_PENDING],
    )
    def test_non_terminal_statuses_raise(self, bad_status: CallStatus) -> None:
        with pytest.raises(ValueError, match="non-terminal outcome"):
            classify(bad_status)


class TestTransitionResult:
    def test_no_op(self) -> None:
        r = TransitionResult.no_op()
        assert r.applied is False
        assert r.row is None
        assert r.is_no_op() is True

    def test_applied(self) -> None:
        row = {"id": uuid4(), "status": "DIALING"}
        r = TransitionResult.applied_(row)
        assert r.applied is True
        assert r.row == row
        assert r.is_no_op() is False


class TestTransitionColumnUpdateGuard:
    # Guard on the allow-list runs BEFORE any DB work. Using AsyncMock lets us
    # assert no SQL was ever issued by inspecting call_count on conn.fetchrow.

    async def test_unauthorized_column_raises_before_db(self) -> None:
        conn = MagicMock()
        conn.fetchrow = AsyncMock()
        conn.execute = AsyncMock()

        with pytest.raises(ValueError, match="unauthorized column update: evil_col"):
            await transition(
                conn,
                call_id=uuid4(),
                expected_status=CallStatus.QUEUED,
                new_status=CallStatus.DIALING,
                expected_epoch=0,
                new_epoch=1,
                event_type="TRANSITION",
                reason="test",
                column_updates={"evil_col": "x"},
            )

        conn.fetchrow.assert_not_called()
        conn.execute.assert_not_called()

    async def test_multiple_unauthorized_keys_reports_deterministic(self) -> None:
        conn = MagicMock()
        conn.fetchrow = AsyncMock()

        # Two invalid keys: sorted-first one surfaces so the error message is
        # stable regardless of dict insertion order.
        with pytest.raises(ValueError, match="unauthorized column update: a_bad"):
            await transition(
                conn,
                call_id=uuid4(),
                expected_status=CallStatus.QUEUED,
                new_status=CallStatus.DIALING,
                expected_epoch=0,
                event_type="TRANSITION",
                reason="test",
                column_updates={"z_bad": "x", "a_bad": "y"},
            )
        conn.fetchrow.assert_not_called()

    async def test_authorized_columns_do_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        call_id = uuid4()
        campaign_id = uuid4()

        async def fake_emit(conn: Any, event: Any) -> None:
            return None

        monkeypatch.setattr(state_machine, "emit_audit", fake_emit)
        monkeypatch.setattr(
            state_machine,
            "maybe_promote_to_active",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            state_machine,
            "maybe_transition_campaign_terminal",
            AsyncMock(return_value=None),
        )

        returned_row = {
            "id": call_id,
            "campaign_id": campaign_id,
            "status": "DIALING",
            "attempt_epoch": 1,
        }
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value=returned_row)

        result = await transition(
            conn,
            call_id=call_id,
            expected_status=CallStatus.QUEUED,
            new_status=CallStatus.DIALING,
            expected_epoch=0,
            new_epoch=1,
            event_type="TRANSITION",
            reason="claim",
            column_updates={"provider_call_id": "pc-1"},
        )

        assert result.applied is True
        assert result.row == returned_row
        conn.fetchrow.assert_awaited_once()


class TestTransitionBehavior:
    # These tests exercise control-flow around the single conn.fetchrow call
    # using a mock. They document: when CAS no-ops, no audit or side-effects
    # fire; when CAS applies, exactly one audit fires; and campaign side-
    # effects only fire on the right status transitions.

    async def test_no_op_skips_audit_and_side_effects(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        emit_mock = AsyncMock()
        promote_mock = AsyncMock()
        terminal_mock = AsyncMock()
        monkeypatch.setattr(state_machine, "emit_audit", emit_mock)
        monkeypatch.setattr(state_machine, "maybe_promote_to_active", promote_mock)
        monkeypatch.setattr(state_machine, "maybe_transition_campaign_terminal", terminal_mock)

        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value=None)

        result = await transition(
            conn,
            call_id=uuid4(),
            expected_status=CallStatus.DIALING,
            new_status=CallStatus.COMPLETED,
            expected_epoch=5,
            event_type="TRANSITION",
            reason="late webhook",
        )

        assert result.is_no_op()
        emit_mock.assert_not_awaited()
        promote_mock.assert_not_awaited()
        terminal_mock.assert_not_awaited()

    async def test_terminal_status_triggers_campaign_rollup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        emit_mock = AsyncMock()
        promote_mock = AsyncMock()
        terminal_mock = AsyncMock()
        monkeypatch.setattr(state_machine, "emit_audit", emit_mock)
        monkeypatch.setattr(state_machine, "maybe_promote_to_active", promote_mock)
        monkeypatch.setattr(state_machine, "maybe_transition_campaign_terminal", terminal_mock)

        campaign_id = uuid4()
        call_id = uuid4()
        conn = MagicMock()
        conn.fetchrow = AsyncMock(
            return_value={
                "id": call_id,
                "campaign_id": campaign_id,
                "status": "COMPLETED",
                "attempt_epoch": 1,
            }
        )

        await transition(
            conn,
            call_id=call_id,
            expected_status=CallStatus.IN_PROGRESS,
            new_status=CallStatus.COMPLETED,
            expected_epoch=1,
            event_type="TRANSITION",
            reason="provider completed",
        )

        emit_mock.assert_awaited_once()
        promote_mock.assert_not_awaited()
        terminal_mock.assert_awaited_once_with(conn, campaign_id)

    async def test_queued_to_dialing_triggers_promote_not_rollup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        emit_mock = AsyncMock()
        promote_mock = AsyncMock()
        terminal_mock = AsyncMock()
        monkeypatch.setattr(state_machine, "emit_audit", emit_mock)
        monkeypatch.setattr(state_machine, "maybe_promote_to_active", promote_mock)
        monkeypatch.setattr(state_machine, "maybe_transition_campaign_terminal", terminal_mock)

        campaign_id = uuid4()
        call_id = uuid4()
        conn = MagicMock()
        conn.fetchrow = AsyncMock(
            return_value={
                "id": call_id,
                "campaign_id": campaign_id,
                "status": "DIALING",
                "attempt_epoch": 1,
            }
        )

        await transition(
            conn,
            call_id=call_id,
            expected_status=CallStatus.QUEUED,
            new_status=CallStatus.DIALING,
            expected_epoch=0,
            new_epoch=1,
            event_type="CLAIMED",
            reason="claim",
        )

        promote_mock.assert_awaited_once_with(conn, campaign_id)
        terminal_mock.assert_not_awaited()

    async def test_same_status_update_does_not_trigger_promote(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # DIALING -> DIALING (recording provider_call_id) must not re-fire
        # maybe_promote_to_active — that would emit a spurious audit row.
        emit_mock = AsyncMock()
        promote_mock = AsyncMock()
        terminal_mock = AsyncMock()
        monkeypatch.setattr(state_machine, "emit_audit", emit_mock)
        monkeypatch.setattr(state_machine, "maybe_promote_to_active", promote_mock)
        monkeypatch.setattr(state_machine, "maybe_transition_campaign_terminal", terminal_mock)

        campaign_id = uuid4()
        call_id = uuid4()
        conn = MagicMock()
        conn.fetchrow = AsyncMock(
            return_value={
                "id": call_id,
                "campaign_id": campaign_id,
                "status": "DIALING",
                "attempt_epoch": 1,
                "provider_call_id": "pc-1",
            }
        )

        await transition(
            conn,
            call_id=call_id,
            expected_status=CallStatus.DIALING,
            new_status=CallStatus.DIALING,
            expected_epoch=1,
            event_type="TRANSITION",
            reason="record provider call id",
            column_updates={"provider_call_id": "pc-1"},
        )

        emit_mock.assert_awaited_once()
        promote_mock.assert_not_awaited()
        terminal_mock.assert_not_awaited()

    async def test_status_can_be_str_or_enum(self, monkeypatch: pytest.MonkeyPatch) -> None:
        emit_mock = AsyncMock()
        monkeypatch.setattr(state_machine, "emit_audit", emit_mock)
        monkeypatch.setattr(state_machine, "maybe_promote_to_active", AsyncMock(return_value=None))
        monkeypatch.setattr(
            state_machine,
            "maybe_transition_campaign_terminal",
            AsyncMock(return_value=None),
        )

        conn = MagicMock()
        conn.fetchrow = AsyncMock(
            return_value={
                "id": uuid4(),
                "campaign_id": uuid4(),
                "status": "DIALING",
                "attempt_epoch": 1,
            }
        )

        # Pass raw strings; the function must coerce without blowing up.
        await transition(
            conn,
            call_id=uuid4(),
            expected_status="QUEUED",
            new_status="DIALING",
            expected_epoch=0,
            new_epoch=1,
            event_type="CLAIMED",
            reason="claim",
        )

        audit_event = emit_mock.await_args.args[1]
        assert audit_event.state_before == "QUEUED"
        assert audit_event.state_after == "DIALING"
