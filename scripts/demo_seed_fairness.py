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
) -> dict[str, Any]:
    body = {
        "name": name,
        "timezone": "UTC",
        "schedule": _SCHEDULE_247,
        "max_concurrent": max_concurrent,
        "retry_config": {"max_attempts": max_attempts, "backoff_base_seconds": 5},
        "phones": phones,
    }
    resp = client.post("/campaigns", json=body)
    resp.raise_for_status()
    return resp.json()  # type: ignore[no-any-return]


def main() -> int:
    t0 = datetime.now(tz=UTC)
    with httpx.Client(base_url=BASE_URL, timeout=15.0) as client:
        a = _post_campaign(
            client,
            name="fairness-A-heavy",
            phones=_phones("5500000", 15),
            max_concurrent=3,
            max_attempts=2,
        )
        b = _post_campaign(
            client,
            name="fairness-B-light",
            phones=_phones("5600000", 5),
            max_concurrent=2,
            max_attempts=2,
        )
        a_id, b_id = a["id"], b["id"]
        print(f"[fairness] created campaigns A={a_id} (15 phones, cap=3)")
        print(f"[fairness]                    B={b_id} (5 phones,  cap=2)")

    t_end = (t0 + timedelta(seconds=30)).isoformat()
    t_start = t0.isoformat()
    urls = [
        (
            f"{BASE_URL}/ui/#audit?event_type=DISPATCH&from_ts={t_start}&to_ts={t_end}",
            "During the first ~30s (while both campaigns have queued work) "
            "per-campaign DISPATCH counts track the max_concurrent ratio "
            "(A=3, B=2) — the cap, not the backlog size, decides throughput. "
            "After B drains, only A continues.",
        ),
        (
            f"{BASE_URL}/ui/#audit?campaign_id={a_id}&event_type=RETRY_DUE,DISPATCH",
            "Every RETRY_DUE row is followed within the next tick by a "
            "DISPATCH on the same call_id. Retries beat new calls at the "
            "system level — the scheduler picks the retry-due campaign over "
            "anyone else's fresh queue.",
        ),
        (
            f"{BASE_URL}/ui/#audit?campaign_id={a_id}&event_type=CLAIMED,TRANSITION",
            "Every CLAIMED row follows the nearest prior terminal TRANSITION "
            "(COMPLETED/FAILED/NO_ANSWER/BUSY) on the same campaign within "
            "~1s — safety-net + wake-notify latency. That is continuous "
            "channel reuse: the moment a call completes, the next one is "
            "claimed, not deferred to a batch boundary. Each CLAIMED's "
            "extra.in_flight_at_claim is <= max_concurrent - 1.",
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
