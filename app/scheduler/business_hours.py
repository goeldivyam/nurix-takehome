from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

_DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True, slots=True)
class TimeWindow:
    start: time
    end: time


class InvalidScheduleError(ValueError):
    # Raised when a campaign's schedule JSON is malformed. Callers (API
    # boundary) are expected to convert to 422 before write; the scheduler
    # treats an invalid schedule as "no work today" (skip + audit).
    pass


def parse_window(raw: dict[str, Any]) -> TimeWindow:
    start = time.fromisoformat(raw["start"])
    end = time.fromisoformat(raw["end"])
    if not (start < end):
        raise InvalidScheduleError(f"window start >= end: {raw!r}")
    return TimeWindow(start=start, end=end)


def parse_day_windows(day_list: Any) -> list[TimeWindow]:
    if day_list is None:
        return []
    if not isinstance(day_list, list):
        raise InvalidScheduleError(f"expected list, got {type(day_list).__name__}")
    return [parse_window(w) for w in day_list]


def is_in_window(schedule: dict[str, Any], timezone: str, now_utc: datetime) -> bool:
    # Returns True iff `now_utc` — converted into the campaign's timezone —
    # falls inside any [start, end) window on the current local weekday.
    # Empty day list → False. No midnight-wrap; 22:00-02:00 must be split
    # into two rows at the API boundary.
    try:
        tz = ZoneInfo(timezone)
    except Exception:
        # Unknown timezone string — reject by returning False so the
        # scheduler simply doesn't dispatch. API validation is the right
        # place to catch this before it ever reaches the tick.
        return False

    local = now_utc.astimezone(tz)
    day_key = _DAY_KEYS[local.weekday()]
    day_raw = schedule.get(day_key, [])
    try:
        windows = parse_day_windows(day_raw)
    except InvalidScheduleError:
        return False

    t_now = local.time()
    return any(w.start <= t_now < w.end for w in windows)
