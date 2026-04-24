from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.audit.reader import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    PHONE_FILTER_MIN_DIGITS,
    decode_cursor,
    encode_cursor,
    normalize_phone_query,
    query_audit,
)


class TestCursor:
    def test_round_trip_preserves_ts_and_id(self) -> None:
        ts = datetime(2026, 4, 23, 16, 12, 45, 123456, tzinfo=UTC)
        token = encode_cursor(ts, 42)
        ts_back, id_back = decode_cursor(token)
        assert ts_back == ts
        assert id_back == 42

    def test_round_trip_preserves_microseconds(self) -> None:
        ts = datetime(2026, 4, 23, 16, 12, 45, 987654, tzinfo=UTC)
        ts_back, _ = decode_cursor(encode_cursor(ts, 1))
        assert ts_back.microsecond == 987654

    def test_cursor_is_urlsafe_base64(self) -> None:
        ts = datetime(2026, 4, 23, 16, 12, 45, tzinfo=UTC)
        token = encode_cursor(ts, 1)
        # urlsafe encoding only emits A-Z a-z 0-9 - _ =
        assert all(c.isalnum() or c in "-_=" for c in token)


class TestNormalizePhoneQuery:
    # The normalizer is the security + UX boundary for the operator phone
    # filter — it decides what an empty filter looks like (None → no-op),
    # how a formatted human input is canonicalized ("+1 (415) 555-1234"
    # → "14155551234"), and at what threshold the filter silently refuses
    # to fire (minimum digits, to prevent a one-digit accidental scan of
    # every phone-carrying audit row). The threshold is advertised as a
    # module constant so this test can reason about it symbolically.

    def test_none_returns_none(self) -> None:
        assert normalize_phone_query(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert normalize_phone_query("") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert normalize_phone_query("   ") is None

    def test_below_threshold_returns_none(self) -> None:
        # Threshold is PHONE_FILTER_MIN_DIGITS; any input with fewer
        # effective digits than that must no-op. Avoids a naive "+1" input
        # matching thousands of rows by accident.
        assert PHONE_FILTER_MIN_DIGITS >= 3
        assert normalize_phone_query("+1") is None
        assert normalize_phone_query("12") is None

    def test_exactly_at_threshold_passes(self) -> None:
        # Exactly at the floor is the boundary the docstring promises.
        assert normalize_phone_query("123") == "123"

    def test_formatted_input_canonicalises_to_digits_only(self) -> None:
        # Operators paste from CSVs, leads tools, customer tickets — all
        # possible shapes for the same number must collapse to the same
        # query so the URL round-trips canonically.
        assert normalize_phone_query("+1 (415) 555-1234") == "14155551234"
        assert normalize_phone_query("415-555-1234") == "4155551234"
        assert normalize_phone_query("415.555.1234") == "4155551234"
        assert normalize_phone_query(" +1 415 555 1234 ") == "14155551234"

    def test_letters_stripped_same_as_other_non_digits(self) -> None:
        # Pure noise input strips to nothing -> below threshold -> None.
        assert normalize_phone_query("abc-xyz") is None
        # Alphanumeric mix extracts only the digit run and applies the
        # threshold check on the remainder.
        assert normalize_phone_query("phone 5551234") == "5551234"


class TestLimitBounds:
    async def test_limit_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="limit"):
            await query_audit(_StubPool(), limit=0)

    async def test_limit_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="limit"):
            await query_audit(_StubPool(), limit=-1)

    async def test_limit_above_cap_raises(self) -> None:
        with pytest.raises(ValueError, match="limit"):
            await query_audit(_StubPool(), limit=MAX_LIMIT + 1)

    async def test_default_limit_is_within_cap(self) -> None:
        # The default matches rubric-level expectations (reasonable page
        # without hammering the DB).
        assert 0 < DEFAULT_LIMIT <= MAX_LIMIT


class _StubPool:
    # Rejection paths (limit guards) fire before any pool work — nothing gets
    # called here, but mypy / ruff are happier with a typed stub.

    async def acquire(self) -> object:  # pragma: no cover — never reached in these tests
        raise AssertionError("limit guard should have prevented pool acquire")
