from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.persistence.repositories import (
    _decode_campaign_cursor,
    _encode_campaign_cursor,
    _loads_json,
)

# Audit cursor + list-limit coverage lives in tests/unit/test_audit.py
# alongside `app.audit.reader`, the single owner of that surface.


class TestCampaignCursor:
    def test_round_trip_preserves_created_at_and_id(self) -> None:
        created_at = datetime(2026, 3, 15, 9, 0, 0, tzinfo=UTC)
        campaign_id = uuid4()
        token = _encode_campaign_cursor(created_at, campaign_id)
        decoded_ts, decoded_id = _decode_campaign_cursor(token)
        assert decoded_ts == created_at
        assert decoded_id == campaign_id


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
