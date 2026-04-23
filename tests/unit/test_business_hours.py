from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.scheduler.business_hours import (
    InvalidScheduleError,
    is_in_window,
    parse_window,
)

# -- Schedules reused across tests ------------------------------------------

# Monday 09:00-17:00 in UTC. 2026-04-20 is a Monday in the Gregorian calendar;
# we pick that date for concrete datetimes below.
MON_9_TO_5_UTC: dict[str, Any] = {
    "mon": [{"start": "09:00", "end": "17:00"}],
}

# Split-window schedule: morning + afternoon block on Monday.
MON_SPLIT_UTC: dict[str, Any] = {
    "mon": [
        {"start": "09:00", "end": "12:00"},
        {"start": "14:00", "end": "17:00"},
    ],
}

# Same window keyed to America/Los_Angeles local time.
MON_9_TO_5_LA: dict[str, Any] = {
    "mon": [{"start": "09:00", "end": "17:00"}],
}


def _utc(year: int, month: int, day: int, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


class TestInclusiveStartExclusiveEnd:
    # Contract: [start, end) — start is inside the window, end is outside.
    # Using 2026-04-20 (a Monday) to pin the weekday key.

    def test_start_boundary_inclusive(self) -> None:
        assert is_in_window(MON_9_TO_5_UTC, "UTC", _utc(2026, 4, 20, 9, 0, 0)) is True

    def test_end_boundary_exclusive(self) -> None:
        assert is_in_window(MON_9_TO_5_UTC, "UTC", _utc(2026, 4, 20, 17, 0, 0)) is False

    def test_just_before_end(self) -> None:
        assert is_in_window(MON_9_TO_5_UTC, "UTC", _utc(2026, 4, 20, 16, 59, 59)) is True

    def test_just_before_start(self) -> None:
        assert is_in_window(MON_9_TO_5_UTC, "UTC", _utc(2026, 4, 20, 8, 59, 59)) is False


class TestEmptyAndMissingDays:
    def test_empty_day_list_returns_false(self) -> None:
        # Tuesday has no windows at all on this schedule.
        schedule: dict[str, Any] = {"tue": []}
        # 2026-04-21 is a Tuesday — empty list → nothing matches.
        assert is_in_window(schedule, "UTC", _utc(2026, 4, 21, 10, 0, 0)) is False

    def test_missing_day_key_returns_false(self) -> None:
        # Schedule has Monday only; check Sunday — missing key defaults to [].
        # 2026-04-19 is a Sunday.
        assert is_in_window(MON_9_TO_5_UTC, "UTC", _utc(2026, 4, 19, 10, 0, 0)) is False


class TestMultiWindowSameDay:
    def test_between_windows_false(self) -> None:
        # 13:00 is between the two Monday windows → False.
        assert is_in_window(MON_SPLIT_UTC, "UTC", _utc(2026, 4, 20, 13, 0, 0)) is False

    def test_afternoon_window_start_true(self) -> None:
        # 14:00 is the inclusive start of the afternoon window → True.
        assert is_in_window(MON_SPLIT_UTC, "UTC", _utc(2026, 4, 20, 14, 0, 0)) is True

    def test_morning_window_hit(self) -> None:
        assert is_in_window(MON_SPLIT_UTC, "UTC", _utc(2026, 4, 20, 10, 0, 0)) is True


class TestTimezoneConversion:
    # Campaign timezone is America/Los_Angeles. The window "mon 09:00-17:00"
    # is in local time, so we must convert `now_utc` into LA time first.

    def test_la_9_30_local_is_in_window(self) -> None:
        # 2026-04-20 16:30 UTC == 09:30 local in LA on Monday (PDT, UTC-7).
        assert is_in_window(MON_9_TO_5_LA, "America/Los_Angeles", _utc(2026, 4, 20, 16, 30)) is True

    def test_la_before_monday_local_still_sunday(self) -> None:
        # 2026-04-20 08:30 UTC == 01:30 local in LA — STILL Sunday in LA.
        # The Monday window should not match because LA's local weekday is Sunday.
        assert is_in_window(MON_9_TO_5_LA, "America/Los_Angeles", _utc(2026, 4, 20, 8, 30)) is False


class TestUnknownTimezone:
    def test_unknown_timezone_returns_false_gracefully(self) -> None:
        # Garbage timezone string — the predicate must not propagate the
        # ZoneInfoNotFoundError; it just refuses to dispatch.
        assert is_in_window(MON_9_TO_5_UTC, "Not/A_Real_Tz", _utc(2026, 4, 20, 10, 0, 0)) is False


class TestParseWindowValidation:
    def test_parse_window_start_equals_end_raises(self) -> None:
        with pytest.raises(InvalidScheduleError):
            parse_window({"start": "09:00", "end": "09:00"})

    def test_parse_window_start_after_end_raises(self) -> None:
        with pytest.raises(InvalidScheduleError):
            parse_window({"start": "17:00", "end": "09:00"})

    def test_is_in_window_with_malformed_day_returns_false(self) -> None:
        # A malformed window somewhere in the day must not propagate — the
        # scheduler treats the day as no-work and moves on.
        bad_schedule: dict[str, Any] = {"mon": [{"start": "17:00", "end": "09:00"}]}
        assert is_in_window(bad_schedule, "UTC", _utc(2026, 4, 20, 10, 0, 0)) is False
