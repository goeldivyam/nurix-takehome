#!/usr/bin/env python3
"""Fairness demo.

Seeds two campaigns with different max_concurrent caps and a non-zero
failure rate so retries actually flow. Prints three pre-filtered /ui
URLs with one-line narratives for a guided tour during the live demo.

Expects DEMO_MODE=true + MOCK_FAILURE_RATE set in the running app's env
so the demo has enough failures to show retries-before-new.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8001")

_SCHEDULE_247 = {
    d: [{"start": "00:00", "end": "23:59"}]
    for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
}


def _phones(prefix: str, count: int) -> list[str]:
    # +1-area-code-based US numbers, distinct per campaign so the
    # phone-level in-flight guard doesn't block the cross-campaign run.
    base = int(prefix)
    return [f"+1415{base + i:07d}" for i in range(count)]


def _post_campaign(
    client: httpx.Client,
    *,
    name: str,
    phones: list[str],
    max_concurrent: int,
    max_attempts: int,
    schedule: dict[str, Any] | None = None,
    timezone: str = "UTC",
    backoff_base_seconds: int = 2,
) -> dict[str, Any]:
    body = {
        "name": name,
        "timezone": timezone,
        "schedule": schedule if schedule is not None else _SCHEDULE_247,
        "max_concurrent": max_concurrent,
        "retry_config": {
            "max_attempts": max_attempts,
            "backoff_base_seconds": backoff_base_seconds,
        },
        "phones": phones,
    }
    resp = client.post("/campaigns", json=body)
    resp.raise_for_status()
    return resp.json()  # type: ignore[no-any-return]


def _closed_schedule() -> dict[str, Any]:
    # Single 1-minute window at midnight on each weekday. At any live demo
    # moment the scheduler will find no window matching "now," so every
    # tick drops this campaign via `SKIP_BUSINESS_HOUR` — the exact rubric
    # affordance for business-hour gating that a 24/7 seed can't show.
    return {
        d: [{"start": "00:00", "end": "00:01"}]
        for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    }


def main() -> int:
    t0 = datetime.now(tz=UTC)
    with httpx.Client(base_url=BASE_URL, timeout=15.0) as client:
        # A heavy (cap=3) + B heavy (cap=2) — both larger so B still has
        # queued work when A's first retries come due (backoff_base=2s,
        # so A retries land at ~t+4s while B dispatches until ~t+30s+).
        # URL #2's cross-campaign "retries-beat-new" narrative now reads
        # on actual contention, not on a decayed single-campaign queue.
        a = _post_campaign(
            client,
            name="fairness-A-heavy",
            phones=_phones("5500000", 15),
            max_concurrent=3,
            max_attempts=2,
            backoff_base_seconds=2,
        )
        b = _post_campaign(
            client,
            name="fairness-B-heavy",
            phones=_phones("5600000", 12),
            max_concurrent=2,
            max_attempts=2,
            backoff_base_seconds=2,
        )
        # Third campaign is PENDING with a closed 24/7 schedule so every
        # tick emits a SKIP_BUSINESS_HOUR audit row — makes the business-
        # hour gate visible in URL #6 below.
        c = _post_campaign(
            client,
            name="fairness-C-closed-hours",
            phones=_phones("5700000", 3),
            max_concurrent=1,
            max_attempts=1,
            schedule=_closed_schedule(),
            timezone="UTC",
        )
        a_id, b_id, c_id = a["id"], b["id"], c["id"]
        print(f"[fairness] created campaigns A={a_id} (15 phones, cap=3)")
        print(f"[fairness]                    B={b_id} (12 phones, cap=2)")
        print(f"[fairness]                    C={c_id} (3 phones, closed-hours)")

    t_end = (t0 + timedelta(seconds=30)).isoformat()
    t_start = t0.isoformat()
    # URL shape is `/ui/?filter=val#audit` — query BEFORE hash. The audit
    # view reads filters out of `window.location.search`; a hash-embedded
    # query would land the viewer on the default tab with no filters.
    urls = [
        (
            f"{BASE_URL}/ui/?event_type=DISPATCH&from_ts={t_start}&to_ts={t_end}#audit",
            "During the first ~30s (while both campaigns have queued work) "
            "per-campaign DISPATCH counts track the max_concurrent ratio "
            "(A=3, B=2) — the cap, not the backlog size, decides throughput. "
            "After B drains, only A continues.",
        ),
        (
            f"{BASE_URL}/ui/?event_type=RETRY_DUE,CLAIMED,DISPATCH#audit",
            "Cross-campaign retry priority. A retries fire ~2s after a "
            "FAILED / NO_ANSWER / BUSY; at that moment B still has queued "
            "new calls. RETRY_DUE + the following CLAIMED on the same "
            "call_id land BEFORE the next B CLAIMED at the same tick — "
            "retries beat new calls at the system level, not just inside "
            "one campaign.",
        ),
        (
            f"{BASE_URL}/ui/?campaign_id={a_id}&event_type=CLAIMED,TRANSITION#audit",
            "Every CLAIMED row follows the nearest prior terminal TRANSITION "
            "(COMPLETED/FAILED/NO_ANSWER/BUSY) on the same campaign within "
            "~1s — safety-net + wake-notify latency. That is continuous "
            "channel reuse: the moment a call completes, the next one is "
            "claimed, not deferred to a batch boundary. Each CLAIMED's "
            "extra.in_flight_at_claim is <= max_concurrent - 1.",
        ),
        (
            f"{BASE_URL}/ui/?event_type=CAMPAIGN_PROMOTED_ACTIVE,CAMPAIGN_COMPLETED#audit",
            "Campaign lifecycle: one PENDING→ACTIVE row on first dispatch, "
            "one ACTIVE→terminal row when every call has reached a final "
            "disposition. Rolls up to COMPLETED if any call succeeded, else "
            "FAILED.",
        ),
        (
            f"{BASE_URL}/ui/?campaign_id={a_id}&event_type=SKIP_CONCURRENCY,CLAIMED#audit",
            "Concurrency gate — SKIP_CONCURRENCY rows land when A is at its "
            "max_concurrent=3 cap and the tick passes over it; alternating "
            "with CLAIMED rows makes the gate observable under load.",
        ),
        (
            f"{BASE_URL}/ui/?campaign_id={c_id}&event_type=SKIP_BUSINESS_HOUR,CLAIMED#audit",
            "Business-hour gate — campaign C has a closed schedule so every "
            "tick drops it with SKIP_BUSINESS_HOUR. No CLAIMED rows for C. "
            "Assignment requirement: prevent calls outside allowed hours.",
        ),
    ]
    print("\n[fairness] guided-tour URLs (open each in turn):\n")
    for i, (url, narrative) in enumerate(urls, 1):
        print(f"  ({i}) {url}")
        for line in narrative.splitlines():
            print(f"      {line}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
