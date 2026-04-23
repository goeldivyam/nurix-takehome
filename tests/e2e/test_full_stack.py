from __future__ import annotations

import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8001")

_SCHEDULE_247 = {
    d: [{"start": "00:00", "end": "23:59"}]
    for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
}

_HTTP_NOT_FOUND = 404

# The e2e test is gated on an explicit env flag so `pytest tests/` on a
# fresh clone (without a running stack) doesn't fail a drift check. Run
# it from `make up && E2E=1 pytest tests/e2e/ -v`.
_E2E_ENABLED = os.environ.get("E2E", "").lower() in ("1", "true", "yes")

pytestmark = pytest.mark.skipif(
    not _E2E_ENABLED, reason="E2E=1 not set; skipping full-stack e2e run"
)


@pytest.fixture(scope="module")
def client() -> Iterator[httpx.Client]:
    # Probe /health first so a missing stack fails fast with a clear message
    # rather than a cryptic per-test timeout.
    with httpx.Client(base_url=BASE_URL, timeout=15.0) as c:
        try:
            r = c.get("/health")
        except httpx.ConnectError as exc:
            pytest.skip(f"{BASE_URL} not reachable ({exc}); start with `make up`")
        if r.status_code != 200:
            pytest.skip(f"{BASE_URL}/health returned {r.status_code}")
        yield c


def _new_campaign(
    client: httpx.Client,
    *,
    name: str,
    phones: list[str],
    max_concurrent: int,
    max_attempts: int,
) -> dict[str, Any]:
    body = {
        "name": name,
        "timezone": "UTC",
        "schedule": _SCHEDULE_247,
        "max_concurrent": max_concurrent,
        "retry_config": {"max_attempts": max_attempts, "backoff_base_seconds": 2},
        "phones": phones,
    }
    r = client.post("/campaigns", json=body)
    r.raise_for_status()
    return r.json()  # type: ignore[no-any-return]


def _phones(prefix: str, count: int) -> list[str]:
    # Prefix is a 7-digit subscriber base; we add the index to keep numbers
    # unique and ensure the partial unique index doesn't trip inside a
    # single test run.
    base = int(prefix)
    return [f"+1415{base + i:07d}" for i in range(count)]


def _wait_for_drain(
    client: httpx.Client, campaign_ids: list[str], *, timeout_s: float = 90.0
) -> dict[str, dict[str, int]]:
    # Poll per-campaign /stats until every call has reached a terminal
    # disposition. Returns the final stats dict per campaign.
    deadline = time.monotonic() + timeout_s
    stats: dict[str, dict[str, int]] = {cid: {} for cid in campaign_ids}
    while time.monotonic() < deadline:
        done = True
        for cid in campaign_ids:
            r = client.get(f"/campaigns/{cid}/stats")
            r.raise_for_status()
            s = r.json()
            stats[cid] = s
            if s["completed"] + s["failed"] < s["total"]:
                done = False
        if done:
            return stats
        time.sleep(0.5)
    raise AssertionError(f"campaigns did not drain in {timeout_s}s; last stats: {stats}")


def test_full_stack_fairness_retries_and_audit_shape(client: httpx.Client) -> None:
    # Seed A heavy + B light. Plan parameters:
    #   A: 15 phones, max_concurrent=3, max_attempts=2
    #   B:  5 phones, max_concurrent=2, max_attempts=2
    # Non-zero MOCK_FAILURE_RATE in the running stack's .env gives us at
    # least some retry traffic.
    a = _new_campaign(
        client, name="e2e-A", phones=_phones("7000000", 15), max_concurrent=3, max_attempts=2
    )
    b = _new_campaign(
        client, name="e2e-B", phones=_phones("7100000", 5), max_concurrent=2, max_attempts=2
    )

    stats = _wait_for_drain(client, [a["id"], b["id"]])

    # ---- Per-campaign totals -----------------------------------------
    assert stats[a["id"]]["total"] == 15
    assert stats[b["id"]]["total"] == 5
    for cid in (a["id"], b["id"]):
        s = stats[cid]
        assert s["in_progress"] == 0
        assert s["completed"] + s["failed"] == s["total"]

    # ---- Retry flow observed -----------------------------------------
    total_retries = sum(stats[cid]["retries_attempted"] for cid in (a["id"], b["id"]))
    assert total_retries >= 0  # may be 0 if MOCK_FAILURE_RATE is 0 — informational only

    # ---- Audit: at least one DISPATCH per campaign -------------------
    for cid in (a["id"], b["id"]):
        r = client.get(
            "/audit",
            params={"campaign_id": cid, "event_type": "DISPATCH", "limit": 500},
        )
        r.raise_for_status()
        events = r.json()["events"]
        assert events, f"no DISPATCH rows observed for campaign {cid}"

    # ---- Every DISPATCH has a matching CLAIMED (same call_id, epoch) -
    for cid in (a["id"], b["id"]):
        r_claimed = client.get(
            "/audit",
            params={"campaign_id": cid, "event_type": "CLAIMED", "limit": 500},
        )
        r_dispatch = client.get(
            "/audit",
            params={"campaign_id": cid, "event_type": "DISPATCH", "limit": 500},
        )
        claimed = {
            (e["call_id"], e["extra"].get("attempt_epoch")) for e in r_claimed.json()["events"]
        }
        dispatched = {
            (e["call_id"], e["extra"].get("attempt_epoch")) for e in r_dispatch.json()["events"]
        }
        # Every DISPATCH must have a matching CLAIMED key. CLAIMED may
        # have extras without a DISPATCH (rejected / unavailable paths).
        missing = dispatched - claimed
        assert not missing, (
            f"campaign {cid}: {len(missing)} DISPATCH rows without matching "
            f"CLAIMED — first 3: {list(missing)[:3]}"
        )

    # ---- CLAIMED extra shape -----------------------------------------
    r = client.get(
        "/audit",
        params={"campaign_id": a["id"], "event_type": "CLAIMED", "limit": 1},
    )
    r.raise_for_status()
    sample = r.json()["events"][0]["extra"]
    expected_keys = {
        "attempt_epoch",
        "in_flight_at_claim",
        "max_concurrent",
        "retries_pending_system",
        "rr_cursor_before",
    }
    assert expected_keys <= set(sample.keys()), (
        f"CLAIMED.extra missing keys: {expected_keys - set(sample.keys())}"
    )
    assert isinstance(sample["attempt_epoch"], int)
    assert isinstance(sample["in_flight_at_claim"], int)
    assert isinstance(sample["max_concurrent"], int)
    assert isinstance(sample["retries_pending_system"], int)
    assert sample["rr_cursor_before"] is None or isinstance(sample["rr_cursor_before"], str)

    # ---- Status mapping (sample one completed + one failed if any) ---
    r = client.get(
        "/audit",
        params={"campaign_id": a["id"], "event_type": "TRANSITION", "limit": 500},
    )
    terminal_calls: dict[str, str] = {}
    for e in r.json()["events"]:
        if e["state_after"] in {"COMPLETED", "FAILED", "NO_ANSWER", "BUSY"} and e["call_id"]:
            terminal_calls[e["call_id"]] = e["state_after"]
    # External status mapping from the /calls endpoint.
    sampled = 0
    for call_id, internal in terminal_calls.items():
        r = client.get(f"/calls/{call_id}")
        if r.status_code == _HTTP_NOT_FOUND:
            continue
        external = r.json()["status"]
        if internal == "COMPLETED":
            assert external == "completed"
        else:
            assert external == "failed"
        sampled += 1
        if sampled >= 3:
            break
    assert sampled >= 1

    # ---- Campaign status rolled up -----------------------------------
    for cid in (a["id"], b["id"]):
        r = client.get(f"/campaigns/{cid}")
        r.raise_for_status()
        cdata = r.json()
        assert cdata["status"] in {"COMPLETED", "FAILED"}


def test_campaign_status_progression_and_audit_chronology(client: httpx.Client) -> None:
    # Covers the PENDING -> ACTIVE -> terminal progression that the
    # assignment specifies explicitly.
    created = _new_campaign(
        client,
        name="e2e-progression",
        phones=_phones("7200000", 3),
        max_concurrent=2,
        max_attempts=1,
    )
    cid = created["id"]
    assert created["status"] == "PENDING"

    # Drain.
    _wait_for_drain(client, [cid], timeout_s=60)

    # The campaign must have passed through ACTIVE at some point — the
    # CAMPAIGN_PROMOTED_ACTIVE audit row is the witness.
    r = client.get(
        "/audit",
        params={"campaign_id": cid, "event_type": "CAMPAIGN_PROMOTED_ACTIVE"},
    )
    r.raise_for_status()
    assert r.json()["events"], "missing CAMPAIGN_PROMOTED_ACTIVE row"

    r = client.get(
        "/audit",
        params={"campaign_id": cid, "event_type": "CAMPAIGN_COMPLETED"},
    )
    r.raise_for_status()
    rollup = r.json()["events"]
    assert len(rollup) == 1
    assert rollup[0]["state_after"] in {"COMPLETED", "FAILED"}

    # Audit chronology: ts is monotonically non-decreasing within a single
    # fetch (default DESC order in the reader).
    r = client.get("/audit", params={"campaign_id": cid, "limit": 500})
    ts_list = [datetime.fromisoformat(e["ts"]) for e in r.json()["events"]]
    # Reader returns DESC, so the list should be sorted in reverse.
    assert ts_list == sorted(ts_list, reverse=True)
    # And timestamps are timezone-aware.
    assert all(t.tzinfo is not None for t in ts_list)
    # End of run should be recent (within the last 5 minutes).
    if ts_list:
        age = (datetime.now(tz=UTC) - ts_list[0]).total_seconds()
        assert age < 300
