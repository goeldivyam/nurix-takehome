#!/usr/bin/env python3
"""Reclaim demo.

Seeds one campaign, polls GET /calls/{id} until the call reaches
`in_progress` (the external mapping for internal DIALING), then POSTs
/debug/age-dialing to age the row's updated_at into the past. With
DEMO_MODE=true the reclaim sweep runs every 5s, so the RECLAIM_EXECUTED
(or RECLAIM_SKIPPED_TERMINAL, if a webhook beats the sweep) audit row
should land within ~10s of aging.

Requires DEBUG_ENDPOINTS_ENABLED=true in the running app's environment.
"""

from __future__ import annotations

import os
import sys
import time
from http import HTTPStatus
from typing import Any

import httpx

BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8001")

_SCHEDULE_247 = {
    d: [{"start": "00:00", "end": "23:59"}]
    for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
}


def _post_campaign(client: httpx.Client) -> dict[str, Any]:
    body = {
        "name": "reclaim-demo",
        "timezone": "UTC",
        "schedule": _SCHEDULE_247,
        "max_concurrent": 1,
        "retry_config": {"max_attempts": 1, "backoff_base_seconds": 5},
        "phones": ["+14155557701"],
    }
    resp = client.post("/campaigns", json=body)
    resp.raise_for_status()
    return resp.json()  # type: ignore[no-any-return]


def _first_call_id(client: httpx.Client, campaign_id: str) -> str:
    # Pull from /audit — CLAIMED carries the call_id as soon as the
    # scheduler picks the row, which is what we want to race against.
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        r = client.get(
            "/audit",
            params={"campaign_id": campaign_id, "event_type": "CLAIMED", "limit": 1},
        )
        r.raise_for_status()
        rows = r.json()["events"]
        if rows:
            return str(rows[0]["call_id"])
        time.sleep(0.25)
    raise RuntimeError("timed out waiting for CLAIMED event")


def _wait_for_in_progress(client: httpx.Client, call_id: str) -> None:
    # The external mapping for internal DIALING is "in_progress". Age the
    # row only after the call has actually reached a dialing-class state
    # so the reclaim branch has something to reclaim.
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        r = client.get(f"/calls/{call_id}")
        r.raise_for_status()
        status = r.json()["status"]
        if status == "in_progress":
            return
        if status in {"completed", "failed"}:
            raise RuntimeError(
                f"call reached terminal {status!r} before reclaim could run; "
                "try again with a longer MOCK_CALL_DURATION_SECONDS"
            )
        time.sleep(0.2)
    raise RuntimeError("timed out waiting for call to reach in_progress")


def main() -> int:
    with httpx.Client(base_url=BASE_URL, timeout=10.0) as client:
        campaign = _post_campaign(client)
        campaign_id = campaign["id"]
        print(f"[reclaim] created campaign {campaign_id}")

        call_id = _first_call_id(client, campaign_id)
        print(f"[reclaim] scheduler claimed call {call_id}; waiting for in_progress")

        _wait_for_in_progress(client, call_id)
        print("[reclaim] call is in_progress; aging updated_at by 900s")

        age_resp = client.post(f"/debug/age-dialing/{call_id}", params={"by_seconds": 900})
        if age_resp.status_code == HTTPStatus.FORBIDDEN:
            print(
                "[reclaim] /debug/age-dialing returned 403. Set "
                "DEBUG_ENDPOINTS_ENABLED=true in .env and restart the app."
            )
            return 1
        age_resp.raise_for_status()

        print(
            "[reclaim] waiting for RECLAIM_EXECUTED or RECLAIM_SKIPPED_TERMINAL "
            "(~5-10s under DEMO_MODE)..."
        )
        deadline = time.monotonic() + 45.0
        while time.monotonic() < deadline:
            r = client.get(
                "/audit",
                params={
                    "campaign_id": campaign_id,
                    "event_type": "RECLAIM_EXECUTED,RECLAIM_SKIPPED_TERMINAL",
                    "limit": 1,
                },
            )
            r.raise_for_status()
            rows = r.json()["events"]
            if rows:
                print(f"[reclaim] observed {rows[0]['event_type']}: {rows[0]['reason']}")
                break
            time.sleep(1.0)
        else:
            print("[reclaim] timed out waiting for reclaim event")
            return 1

        # URL query goes BEFORE the hash — audit.js reads filters from
        # window.location.search, not from hash fragments.
        filtered = (
            f"{BASE_URL}/ui/?campaign_id={campaign_id}"
            "&event_type=CLAIMED,DEBUG_AGE_DIALING,RECLAIM_EXECUTED,"
            "RECLAIM_SKIPPED_TERMINAL,TRANSITION#audit"
        )
        print(f"\n[reclaim] open this URL to see the full reclaim story:\n  {filtered}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
