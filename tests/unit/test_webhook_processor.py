from __future__ import annotations

import sys
from datetime import UTC, datetime
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

# See test_reclaim.py for the rationale — the state machine imports emit_audit
# at module load, so we must register a stub before the webhook processor
# imports the state machine.
if "app.audit.emitter" not in sys.modules:
    _stub = ModuleType("app.audit.emitter")

    async def _emit_audit_stub(conn: Any, event: Any) -> None:
        return None

    _stub.emit_audit = _emit_audit_stub  # type: ignore[attr-defined]
    sys.modules["app.audit.emitter"] = _stub

from app.persistence.repositories import CallRow, WebhookInboxRow
from app.provider.types import ProviderEvent
from app.scheduler import webhook_processor as wp
from app.scheduler.webhook_processor import _process_one_row, process_pending_inbox
from app.state.types import CallStatus

# -- test helpers ------------------------------------------------------------


class _FakeTransactionContext:
    async def __aenter__(self) -> _FakeTransactionContext:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeConn:
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


def _inbox_row(
    *,
    payload: dict[str, Any] | None = None,
    processed_at: datetime | None = None,
) -> WebhookInboxRow:
    return WebhookInboxRow(
        id=uuid4(),
        provider="mock",
        provider_event_id=f"evt-{uuid4()}",
        payload=payload or {"provider_call_id": "pc-1", "status": "IN_PROGRESS"},
        headers={},
        received_at=datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC),
        processed_at=processed_at,
    )


def _call_row(
    *,
    status: str = "DIALING",
    attempt_epoch: int = 1,
    provider_call_id: str | None = "pc-1",
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


def _make_deps(
    *,
    parse_event_fn: Any,
    batch_max: int = 50,
) -> Any:
    settings = SimpleNamespace(
        webhook_processor_batch_max=batch_max,
        scheduler_safety_net_seconds=1.0,
    )
    pools = SimpleNamespace(scheduler=_FakePool())
    wake = SimpleNamespace(notify=MagicMock())
    return SimpleNamespace(
        settings=settings,
        pools=pools,
        wake=wake,
        parse_event_fn=parse_event_fn,
    )


# -- _process_one_row tests --------------------------------------------------


class TestProcessOneRow:
    async def test_empty_inbox_returns_empty_without_side_effects(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            wp.WebhookInboxRepo, "claim_unprocessed_one", AsyncMock(return_value=None)
        )
        emit_mock = AsyncMock()
        monkeypatch.setattr(wp, "emit_audit", emit_mock)
        transition_mock = AsyncMock()
        monkeypatch.setattr(wp.state, "transition", transition_mock)

        deps = _make_deps(parse_event_fn=MagicMock())

        outcome = await _process_one_row(deps)
        assert outcome == "empty"
        emit_mock.assert_not_awaited()
        transition_mock.assert_not_awaited()

    async def test_unknown_provider_call_id_emits_stale_audit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        row = _inbox_row(payload={"provider_call_id": "nope", "status": "COMPLETED"})
        monkeypatch.setattr(
            wp.WebhookInboxRepo, "claim_unprocessed_one", AsyncMock(return_value=row)
        )
        monkeypatch.setattr(wp.CallRepo, "get_by_provider_call_id", AsyncMock(return_value=None))
        mark_mock = AsyncMock()
        monkeypatch.setattr(wp.WebhookInboxRepo, "mark_processed", mark_mock)
        emit_mock = AsyncMock()
        monkeypatch.setattr(wp, "emit_audit", emit_mock)
        transition_mock = AsyncMock()
        monkeypatch.setattr(wp.state, "transition", transition_mock)

        parse_fn = MagicMock(
            return_value=ProviderEvent(
                provider_event_id="evt-x",
                provider_call_id="nope",
                status_enum=CallStatus.COMPLETED,
            )
        )
        deps = _make_deps(parse_event_fn=parse_fn)

        outcome = await _process_one_row(deps)

        assert outcome == "stale"
        emit_mock.assert_awaited_once()
        emitted_event = emit_mock.await_args.args[1]
        assert emitted_event.event_type == "WEBHOOK_IGNORED_STALE"
        assert emitted_event.reason == "unknown provider_call_id"
        assert emitted_event.extra["provider_event_id"] == "evt-x"
        assert emitted_event.extra["provider_call_id"] == "nope"
        mark_mock.assert_awaited_once()
        transition_mock.assert_not_awaited()

    async def test_stale_cas_emits_second_stale_audit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # CAS mismatch path: the state machine returns is_no_op()=True, we
        # must persist a stale-audit row with expected_status + expected_epoch
        # in `extra` so forensic queries can reconstruct the race.
        inbox = _inbox_row(payload={"provider_call_id": "pc-1", "status": "COMPLETED"})
        call = _call_row(status="QUEUED", attempt_epoch=5, provider_call_id="pc-1")

        monkeypatch.setattr(
            wp.WebhookInboxRepo, "claim_unprocessed_one", AsyncMock(return_value=inbox)
        )
        monkeypatch.setattr(wp.CallRepo, "get_by_provider_call_id", AsyncMock(return_value=call))
        mark_mock = AsyncMock()
        monkeypatch.setattr(wp.WebhookInboxRepo, "mark_processed", mark_mock)
        emit_mock = AsyncMock()
        monkeypatch.setattr(wp, "emit_audit", emit_mock)
        transition_mock = AsyncMock(return_value=SimpleNamespace(is_no_op=lambda: True))
        monkeypatch.setattr(wp.state, "transition", transition_mock)

        parse_fn = MagicMock(
            return_value=ProviderEvent(
                provider_event_id="evt-1",
                provider_call_id="pc-1",
                status_enum=CallStatus.COMPLETED,
            )
        )
        deps = _make_deps(parse_event_fn=parse_fn)

        outcome = await _process_one_row(deps)

        assert outcome == "stale"
        transition_mock.assert_awaited_once()
        emit_mock.assert_awaited_once()
        emitted = emit_mock.await_args.args[1]
        assert emitted.event_type == "WEBHOOK_IGNORED_STALE"
        assert emitted.call_id == call.id
        assert emitted.campaign_id == call.campaign_id
        assert "CAS no-op" in emitted.reason
        assert emitted.extra["expected_status"] == "QUEUED"
        assert emitted.extra["expected_epoch"] == 5
        assert emitted.extra["event_status"] == "COMPLETED"
        assert emitted.extra["provider_event_id"] == "evt-1"
        mark_mock.assert_awaited_once()

    async def test_applied_transition_marks_processed_and_returns_applied(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inbox = _inbox_row(payload={"provider_call_id": "pc-1", "status": "IN_PROGRESS"})
        call = _call_row(status="DIALING", attempt_epoch=1, provider_call_id="pc-1")

        monkeypatch.setattr(
            wp.WebhookInboxRepo, "claim_unprocessed_one", AsyncMock(return_value=inbox)
        )
        monkeypatch.setattr(wp.CallRepo, "get_by_provider_call_id", AsyncMock(return_value=call))
        mark_mock = AsyncMock()
        monkeypatch.setattr(wp.WebhookInboxRepo, "mark_processed", mark_mock)
        emit_mock = AsyncMock()
        monkeypatch.setattr(wp, "emit_audit", emit_mock)
        transition_mock = AsyncMock(return_value=SimpleNamespace(is_no_op=lambda: False))
        monkeypatch.setattr(wp.state, "transition", transition_mock)

        parse_fn = MagicMock(
            return_value=ProviderEvent(
                provider_event_id="evt-ok",
                provider_call_id="pc-1",
                status_enum=CallStatus.IN_PROGRESS,
            )
        )
        deps = _make_deps(parse_event_fn=parse_fn)

        outcome = await _process_one_row(deps)

        assert outcome == "applied"
        transition_mock.assert_awaited_once()
        kwargs = transition_mock.await_args.kwargs
        assert kwargs["expected_status"] == "DIALING"
        assert kwargs["expected_epoch"] == 1
        assert kwargs["new_status"] is CallStatus.IN_PROGRESS
        assert kwargs["event_type"] == "TRANSITION"
        # No stale audit on the happy path — state.transition already wrote
        # the TRANSITION audit in the same txn.
        emit_mock.assert_not_awaited()
        mark_mock.assert_awaited_once()

    async def test_transition_error_returns_error_and_rolls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If state.transition raises, the txn rolls back and the inbox row
        # stays unclaimed for the next drain. The outcome is "error" so the
        # caller doesn't mistakenly count it as processed.
        inbox = _inbox_row(payload={"provider_call_id": "pc-1", "status": "COMPLETED"})
        call = _call_row(status="DIALING", attempt_epoch=1, provider_call_id="pc-1")

        monkeypatch.setattr(
            wp.WebhookInboxRepo, "claim_unprocessed_one", AsyncMock(return_value=inbox)
        )
        monkeypatch.setattr(wp.CallRepo, "get_by_provider_call_id", AsyncMock(return_value=call))
        mark_mock = AsyncMock()
        monkeypatch.setattr(wp.WebhookInboxRepo, "mark_processed", mark_mock)
        emit_mock = AsyncMock()
        monkeypatch.setattr(wp, "emit_audit", emit_mock)
        transition_mock = AsyncMock(side_effect=RuntimeError("db exploded"))
        monkeypatch.setattr(wp.state, "transition", transition_mock)

        parse_fn = MagicMock(
            return_value=ProviderEvent(
                provider_event_id="evt-err",
                provider_call_id="pc-1",
                status_enum=CallStatus.COMPLETED,
            )
        )
        deps = _make_deps(parse_event_fn=parse_fn)

        outcome = await _process_one_row(deps)

        assert outcome == "error"
        mark_mock.assert_not_awaited()
        emit_mock.assert_not_awaited()


# -- process_pending_inbox tests --------------------------------------------


class TestProcessPendingInbox:
    async def test_cap_bounds_the_drain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[int] = []

        async def fake_process_one(_deps: Any) -> str:
            calls.append(len(calls))
            return "applied"

        monkeypatch.setattr(wp, "_process_one_row", fake_process_one)

        deps = _make_deps(parse_event_fn=MagicMock(), batch_max=50)
        processed = await process_pending_inbox(deps)

        # Cap = 50; we return "applied" forever so the loop hits the cap.
        assert processed == 50
        assert len(calls) == 50
        deps.wake.notify.assert_called_once()

    async def test_breaks_on_empty_outcome(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Drain exhausts after 3 rows (3 applied + 1 empty sentinel).
        outcomes = iter(["applied", "applied", "applied", "empty"])

        async def fake_process_one(_deps: Any) -> str:
            return next(outcomes)

        monkeypatch.setattr(wp, "_process_one_row", fake_process_one)

        deps = _make_deps(parse_event_fn=MagicMock(), batch_max=50)
        processed = await process_pending_inbox(deps)

        assert processed == 3
        deps.wake.notify.assert_called_once()

    async def test_all_stale_does_not_notify(self, monkeypatch: pytest.MonkeyPatch) -> None:
        outcomes = iter(["stale", "stale", "empty"])

        async def fake_process_one(_deps: Any) -> str:
            return next(outcomes)

        monkeypatch.setattr(wp, "_process_one_row", fake_process_one)

        deps = _make_deps(parse_event_fn=MagicMock(), batch_max=50)
        processed = await process_pending_inbox(deps)

        assert processed == 2
        deps.wake.notify.assert_not_called()

    async def test_error_outcomes_count_as_processed_but_do_not_notify(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outcomes = iter(["error", "error", "empty"])

        async def fake_process_one(_deps: Any) -> str:
            return next(outcomes)

        monkeypatch.setattr(wp, "_process_one_row", fake_process_one)

        deps = _make_deps(parse_event_fn=MagicMock(), batch_max=50)
        processed = await process_pending_inbox(deps)

        # Errors are observed (not "empty") so the loop keeps draining, but
        # they don't flip applied_any — the scheduler doesn't need a wake
        # for something that didn't free capacity.
        assert processed == 2
        deps.wake.notify.assert_not_called()
