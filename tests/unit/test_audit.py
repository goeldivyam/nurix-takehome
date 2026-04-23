from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.audit.reader import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor, query_audit


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
