from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.persistence.repositories import (
    AUDIT_LIST_MAX_LIMIT,
    AuditRepo,
    _decode_audit_cursor,
    _decode_campaign_cursor,
    _encode_audit_cursor,
    _encode_campaign_cursor,
    _loads_json,
)


class TestAuditCursor:
    def test_round_trip_preserves_ts_and_id(self) -> None:
        ts = datetime(2026, 4, 23, 12, 34, 56, 789012, tzinfo=UTC)
        row_id = 12345
        token = _encode_audit_cursor(ts, row_id)
        decoded_ts, decoded_id = _decode_audit_cursor(token)
        assert decoded_ts == ts
        assert decoded_id == row_id

    def test_token_is_opaque_url_safe_base64(self) -> None:
        ts = datetime(2026, 1, 1, tzinfo=UTC)
        token = _encode_audit_cursor(ts, 1)
        # Ensure no characters unsafe for URL transport.
        assert "+" not in token
        assert "/" not in token
        assert "\n" not in token


class TestCampaignCursor:
    def test_round_trip_preserves_created_at_and_id(self) -> None:
        created_at = datetime(2026, 3, 15, 9, 0, 0, tzinfo=UTC)
        campaign_id = uuid4()
        token = _encode_campaign_cursor(created_at, campaign_id)
        decoded_ts, decoded_id = _decode_campaign_cursor(token)
        assert decoded_ts == created_at
        assert decoded_id == campaign_id


class TestAuditListLimit:
    async def test_limit_over_cap_raises(self) -> None:
        fake_pool = AsyncMock()
        with pytest.raises(ValueError, match="limit must be"):
            await AuditRepo.list(fake_pool, limit=AUDIT_LIST_MAX_LIMIT + 1)
        # Must not have hit the DB.
        fake_pool.fetch.assert_not_awaited()

    async def test_limit_zero_raises(self) -> None:
        fake_pool = AsyncMock()
        with pytest.raises(ValueError, match="limit must be"):
            await AuditRepo.list(fake_pool, limit=0)
        fake_pool.fetch.assert_not_awaited()

    async def test_limit_at_cap_accepted(self) -> None:
        fake_pool = AsyncMock()
        fake_pool.fetch.return_value = []
        result, next_cursor = await AuditRepo.list(fake_pool, limit=AUDIT_LIST_MAX_LIMIT)
        assert result == []
        assert next_cursor is None
        fake_pool.fetch.assert_awaited_once()


class TestLoadsJson:
    def test_loads_dict_passthrough(self) -> None:
        assert _loads_json({"a": 1}) == {"a": 1}

    def test_loads_str_decodes(self) -> None:
        assert _loads_json('{"a": 1}') == {"a": 1}

    def test_loads_none_returns_empty(self) -> None:
        assert _loads_json(None) == {}

    def test_loads_unexpected_raises(self) -> None:
        with pytest.raises(TypeError):
            _loads_json(123)
