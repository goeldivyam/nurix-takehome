from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from app.config import Settings
from app.provider.base import TelephonyProvider
from app.provider.mock import MockProvider, parse_event, verify_signature
from app.provider.types import ProviderEvent
from app.state.types import CallStatus

Sink = Callable[[dict[str, Any]], Awaitable[None]]


def _settings(
    *,
    duration: float = 0.02,
    failure_rate: float = 0.0,
    no_answer_rate: float = 0.0,
) -> Settings:
    # Build a Settings with knobs tuned for fast deterministic tests.
    # env_file="" so local .env doesn't bleed into the test run.
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        mock_call_duration_seconds=duration,
        mock_failure_rate=failure_rate,
        mock_no_answer_rate=no_answer_rate,
    )


@pytest.fixture
def sink_events() -> tuple[list[dict[str, Any]], Sink]:
    events: list[dict[str, Any]] = []

    async def sink(payload: dict[str, Any]) -> None:
        events.append(payload)

    return events, sink


async def _drain_provider(provider: MockProvider, max_wait: float = 2.0) -> None:
    # Wait for every in-flight simulation task to finish or raise.
    # Snapshot the task set — done-callbacks mutate it during shutdown, and
    # we want to observe every task the provider currently has in flight.
    tasks = list(provider._tasks)
    if not tasks:
        return
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=max_wait)


async def test_mock_provider_conforms_to_protocol(
    sink_events: tuple[list[dict[str, Any]], Sink],
) -> None:
    _events, sink = sink_events
    provider: TelephonyProvider = MockProvider(_settings(), sink)
    # If this assignment type-checks, the structural Protocol is satisfied.
    # Also exercise the methods to confirm they're callable at runtime.
    assert callable(provider.place_call)
    assert callable(provider.get_status)
    assert callable(provider.aclose)
    await provider.aclose()


async def test_place_call_idempotent(
    sink_events: tuple[list[dict[str, Any]], Sink],
) -> None:
    events, sink = sink_events
    provider = MockProvider(_settings(duration=5.0), sink)  # long duration: won't finish
    try:
        h1 = await provider.place_call("idem-1", "+14155551234")
        h2 = await provider.place_call("idem-1", "+14155551234")
        assert h1 == h2
        assert h1.provider_call_id == h2.provider_call_id
        # Exactly one simulation task spawned.
        assert len(provider._tasks) == 1
    finally:
        await provider.aclose()


async def test_simulated_events_stream_to_sink(
    sink_events: tuple[list[dict[str, Any]], Sink],
) -> None:
    events, sink = sink_events
    provider = MockProvider(_settings(duration=0.02), sink)
    try:
        handle = await provider.place_call("idem-ok", "+14155550001")
        await _drain_provider(provider)
    finally:
        await provider.aclose()

    statuses = [e["status"] for e in events]
    assert statuses == ["DIALING", "IN_PROGRESS", "COMPLETED"]

    # provider_call_id matches the returned handle across every event.
    assert {e["provider_call_id"] for e in events} == {handle.provider_call_id}

    # provider_event_id suffixes are strictly monotonic.
    suffixes = [int(e["provider_event_id"].rsplit(":", 1)[1]) for e in events]
    assert suffixes == sorted(suffixes)
    assert len(set(suffixes)) == len(suffixes)


async def test_failure_injection(
    monkeypatch: pytest.MonkeyPatch,
    sink_events: tuple[list[dict[str, Any]], Sink],
) -> None:
    events, sink = sink_events
    # random.random() == 0.0 < any positive failure_rate -> FAILED.
    import app.provider.mock as mock_module

    monkeypatch.setattr(mock_module.random, "random", lambda: 0.0)

    provider = MockProvider(
        _settings(duration=0.02, failure_rate=0.5, no_answer_rate=0.1),
        sink,
    )
    try:
        await provider.place_call("idem-fail", "+14155550002")
        await _drain_provider(provider)
    finally:
        await provider.aclose()

    statuses = [e["status"] for e in events]
    # DIALING then straight to FAILED. No IN_PROGRESS on a failure trajectory.
    assert statuses == ["DIALING", "FAILED"]


async def test_no_answer_injection(
    monkeypatch: pytest.MonkeyPatch,
    sink_events: tuple[list[dict[str, Any]], Sink],
) -> None:
    events, sink = sink_events
    # roll in [failure_rate, failure_rate + no_answer_rate) -> NO_ANSWER.
    import app.provider.mock as mock_module

    monkeypatch.setattr(mock_module.random, "random", lambda: 0.5 + 1e-6)

    provider = MockProvider(
        _settings(duration=0.02, failure_rate=0.5, no_answer_rate=0.3),
        sink,
    )
    try:
        await provider.place_call("idem-noans", "+14155550003")
        await _drain_provider(provider)
    finally:
        await provider.aclose()

    statuses = [e["status"] for e in events]
    assert statuses == ["DIALING", "NO_ANSWER"]


async def test_get_status_tracks_current_state(
    sink_events: tuple[list[dict[str, Any]], Sink],
) -> None:
    _events, sink = sink_events
    # Long duration so we can observe DIALING before the task progresses.
    provider = MockProvider(_settings(duration=5.0), sink)
    try:
        handle = await provider.place_call("idem-track", "+14155550004")
        # Yield once so _simulate can set status = DIALING and emit.
        await asyncio.sleep(0)
        assert await provider.get_status(handle.provider_call_id) == CallStatus.DIALING
    finally:
        await provider.aclose()


async def test_get_status_tracks_terminal(
    sink_events: tuple[list[dict[str, Any]], Sink],
) -> None:
    _events, sink = sink_events
    provider = MockProvider(_settings(duration=0.01), sink)
    try:
        handle = await provider.place_call("idem-terminal", "+14155550005")
        await _drain_provider(provider)
        status = await provider.get_status(handle.provider_call_id)
        assert status == CallStatus.COMPLETED  # default trajectory
    finally:
        await provider.aclose()


async def test_get_status_unknown_raises(
    sink_events: tuple[list[dict[str, Any]], Sink],
) -> None:
    _events, sink = sink_events
    provider = MockProvider(_settings(), sink)
    try:
        with pytest.raises(KeyError):
            await provider.get_status("unknown-id")
    finally:
        await provider.aclose()


async def test_aclose_cancels_in_flight(
    sink_events: tuple[list[dict[str, Any]], Sink],
) -> None:
    _events, sink = sink_events
    provider = MockProvider(_settings(duration=5.0), sink)
    await provider.place_call("idem-a", "+14155550010")
    await provider.place_call("idem-b", "+14155550011")
    assert len(provider._tasks) == 2

    # Let the simulations actually start before we cancel, so we exercise
    # the mid-sleep cancellation path.
    await asyncio.sleep(0.01)
    await provider.aclose()

    # All tasks have finished (cancelled or otherwise) and are removed.
    assert provider._tasks == set()


async def test_parse_event_roundtrip() -> None:
    payload = {
        "provider_event_id": "mock-abc:3",
        "provider_call_id": "mock-abc",
        "status": "IN_PROGRESS",
    }
    event = parse_event(payload)
    assert isinstance(event, ProviderEvent)
    assert event.provider_event_id == "mock-abc:3"
    assert event.provider_call_id == "mock-abc"
    assert event.status_enum is CallStatus.IN_PROGRESS


async def test_parse_event_roundtrips_every_emitted_status(
    sink_events: tuple[list[dict[str, Any]], Sink],
) -> None:
    # End-to-end: anything the mock emits, parse_event must consume.
    events, sink = sink_events
    provider = MockProvider(_settings(duration=0.01), sink)
    try:
        await provider.place_call("idem-roundtrip", "+14155550020")
        await _drain_provider(provider)
    finally:
        await provider.aclose()

    for payload in events:
        parsed = parse_event(payload)
        assert parsed.provider_call_id == payload["provider_call_id"]
        assert parsed.status_enum.value == payload["status"]


async def test_verify_signature_returns_true() -> None:
    assert verify_signature({}, b"") is True
    assert verify_signature({"x-signature": "garbage"}, b"payload-bytes") is True
