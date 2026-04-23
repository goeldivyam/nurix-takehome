from __future__ import annotations

import asyncio
import logging
import random
import secrets
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.config import Settings
from app.provider.types import CallHandle, ProviderEvent
from app.state.types import CallStatus

logger = logging.getLogger(__name__)

# Event sink is the in-process callback the mock uses to deliver simulated
# provider events. Webhook ingest in the real adapters is HTTP-driven; for
# the mock we pass a callable directly so tests and the app share one path
# and we avoid spinning a loopback HTTP client in-process.
EventSink = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class _MockCallState:
    # Per-call simulation state held only in this process. Real adapters
    # (Twilio / Retell) carry no equivalent — they delegate to the vendor.
    provider_call_id: str
    phone: str
    idempotency_key: str
    status: CallStatus = CallStatus.DIALING


class MockProvider:
    # In-process mock implementation of the TelephonyProvider Protocol.
    # Simulates DIALING -> IN_PROGRESS -> COMPLETED (or FAILED / NO_ANSWER)
    # in a tracked background task. Events are delivered to `event_sink` on
    # each state step so the rest of the system can treat them identically
    # to real webhook deliveries.

    def __init__(self, settings: Settings, event_sink: EventSink) -> None:
        self._settings = settings
        self._event_sink = event_sink
        self._tasks: set[asyncio.Task[None]] = set()
        self._by_idem: dict[str, CallHandle] = {}
        self._states: dict[str, _MockCallState] = {}
        self._event_seq = 0
        self._lock = asyncio.Lock()

    async def place_call(self, idempotency_key: str, phone: str) -> CallHandle:
        # Idempotent on `idempotency_key` = f"{call_id}:{attempt_epoch}".
        # A retried place_call with the same key returns the original handle
        # and does NOT spawn a second simulation.
        async with self._lock:
            existing = self._by_idem.get(idempotency_key)
            if existing is not None:
                return existing
            call_id = f"mock-{secrets.token_hex(8)}"
            handle = CallHandle(
                provider_call_id=call_id,
                accepted_at=datetime.now(UTC),
            )
            self._by_idem[idempotency_key] = handle
            self._states[call_id] = _MockCallState(
                provider_call_id=call_id,
                phone=phone,
                idempotency_key=idempotency_key,
            )
            self._spawn(self._simulate(call_id))
            return handle

    async def get_status(self, call_id: str) -> CallStatus:
        # Used by the stuck-reclaim best-effort confirm path. Unknown ids
        # raise so the caller can treat "never placed" as an invariant
        # violation rather than silently return a stale terminal.
        state = self._states.get(call_id)
        if state is None:
            raise KeyError(call_id)
        return state.status

    async def aclose(self) -> None:
        # Cancel every in-flight simulation task and await completion so
        # shutdown never leaves orphaned tasks that Python would destroy
        # with a "Task was destroyed but it is pending!" warning.
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task: asyncio.Task[None] = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("mock provider task failed", exc_info=exc)

    async def _simulate(self, call_id: str) -> None:
        # Draw the terminal outcome up-front so the simulated trajectory is
        # deterministic from here on (tests monkeypatch `random.random`).
        state = self._states[call_id]
        duration = self._settings.mock_call_duration_effective
        failure_rate = self._settings.mock_failure_rate_effective
        no_answer_rate = self._settings.mock_no_answer_rate

        roll = random.random()  # noqa: S311 — mock simulation, not security-sensitive
        if roll < failure_rate:
            terminal = CallStatus.FAILED
        elif roll < failure_rate + no_answer_rate:
            terminal = CallStatus.NO_ANSWER
        else:
            terminal = CallStatus.COMPLETED

        try:
            state.status = CallStatus.DIALING
            await self._emit(call_id, CallStatus.DIALING)
            await asyncio.sleep(duration / 2)

            if terminal == CallStatus.COMPLETED:
                state.status = CallStatus.IN_PROGRESS
                await self._emit(call_id, CallStatus.IN_PROGRESS)
                await asyncio.sleep(duration / 2)

            state.status = terminal
            await self._emit(call_id, terminal)
        except asyncio.CancelledError:
            # aclose() called — propagate so the task records as cancelled.
            raise
        except Exception:
            logger.exception("mock simulation failed for %s", call_id)

    async def _emit(self, call_id: str, status: CallStatus) -> None:
        # `provider_event_id` is monotonic per process and namespaced by
        # call_id so the dedup key in `webhook_inbox` (provider, event_id)
        # remains unique even if two sinks ever share a namespace.
        self._event_seq += 1
        payload: dict[str, Any] = {
            "provider_event_id": f"{call_id}:{self._event_seq}",
            "provider_call_id": call_id,
            "status": status.value,
        }
        await self._event_sink(payload)


def parse_event(payload: dict[str, Any]) -> ProviderEvent:
    # Adapter-owned translation from the mock's on-wire payload shape to
    # the core ProviderEvent value object. Invoked by the webhook processor
    # after dequeuing a row from webhook_inbox.
    return ProviderEvent(
        provider_event_id=payload["provider_event_id"],
        provider_call_id=payload["provider_call_id"],
        status_enum=CallStatus(payload["status"]),
    )


def verify_signature(headers: dict[str, str], raw_body: bytes) -> bool:
    # The mock is in-process-trusted — the sink hands the ingest helper its
    # own synthetic payloads (raw_body=b""), so any HMAC check would 401
    # every event. Real adapters (Twilio / Retell / Vapi) implement genuine
    # HMAC verification in their own module-level function here.
    del headers, raw_body
    return True
