from __future__ import annotations

import random
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.persistence.repositories import CampaignRowWithCursor
from app.scheduler.tick import TickDecision, _rr_sort_key, compute_backoff


def _campaign_row(
    *,
    campaign_id: UUID,
    last_dispatch_at: datetime | None,
) -> CampaignRowWithCursor:
    # Minimal constructor helper; the fields we don't read can be placeholder.
    return CampaignRowWithCursor(
        id=campaign_id,
        name="n",
        status="ACTIVE",
        timezone="UTC",
        schedule={},
        max_concurrent=3,
        retry_config={},
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_dispatch_at=last_dispatch_at,
    )


class TestComputeBackoff:
    # compute_backoff(n, base) = base * 2^(max(0, n-1)) * U(0.8, 1.2)
    # We stabilize the jitter edges by seeding random to known values then
    # verifying the min/max bounds across several samples. The dominant check
    # is that the bounds hold for *any* jitter sample (tested via range).

    def test_attempt_epoch_1_is_base_with_jitter(self) -> None:
        for seed in range(50):
            random.seed(seed)
            value = compute_backoff(attempt_epoch=1, base_seconds=10).total_seconds()
            assert 8.0 <= value <= 12.0

    def test_attempt_epoch_2_doubles_base(self) -> None:
        for seed in range(50):
            random.seed(seed)
            value = compute_backoff(attempt_epoch=2, base_seconds=10).total_seconds()
            assert 16.0 <= value <= 24.0

    def test_attempt_epoch_3_quadruples_base(self) -> None:
        for seed in range(50):
            random.seed(seed)
            value = compute_backoff(attempt_epoch=3, base_seconds=10).total_seconds()
            assert 32.0 <= value <= 48.0

    def test_attempt_epoch_zero_clamps_to_base(self) -> None:
        # If the caller ever invokes with epoch < 1 the exponent clamps to 0
        # so backoff collapses to exactly `base * jitter`. This guards against
        # accidental negative exponents causing sub-second backoffs.
        for seed in range(50):
            random.seed(seed)
            value = compute_backoff(attempt_epoch=0, base_seconds=10).total_seconds()
            assert 8.0 <= value <= 12.0


class TestRrSortKey:
    def test_none_last_dispatch_beats_concrete_value(self) -> None:
        # Campaign without a cursor sorts BEFORE any campaign that has
        # already dispatched, so a brand-new campaign gets its first turn
        # before an older campaign cycles back around.
        new_id = UUID("00000000-0000-0000-0000-000000000001")
        old_id = UUID("00000000-0000-0000-0000-000000000002")
        now = datetime.now(tz=UTC)
        new_c = _campaign_row(campaign_id=new_id, last_dispatch_at=None)
        old_c = _campaign_row(campaign_id=old_id, last_dispatch_at=now)
        ordered = sorted([old_c, new_c], key=_rr_sort_key)
        assert ordered[0].id == new_id

    def test_tie_break_on_uuid_when_timestamps_equal(self) -> None:
        # Two campaigns with identical last_dispatch_at must sort
        # deterministically by UUID (ascending).
        shared_ts = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
        low_id = UUID("00000000-0000-0000-0000-000000000001")
        high_id = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
        high_c = _campaign_row(campaign_id=high_id, last_dispatch_at=shared_ts)
        low_c = _campaign_row(campaign_id=low_id, last_dispatch_at=shared_ts)
        ordered = sorted([high_c, low_c], key=_rr_sort_key)
        assert [c.id for c in ordered] == [low_id, high_id]

    def test_oldest_cursor_first(self) -> None:
        t_old = datetime(2026, 4, 23, 10, 0, 0, tzinfo=UTC)
        t_new = datetime(2026, 4, 23, 11, 0, 0, tzinfo=UTC)
        a = _campaign_row(
            campaign_id=UUID("00000000-0000-0000-0000-000000000002"), last_dispatch_at=t_new
        )
        b = _campaign_row(
            campaign_id=UUID("00000000-0000-0000-0000-000000000001"), last_dispatch_at=t_old
        )
        ordered = sorted([a, b], key=_rr_sort_key)
        assert ordered[0].last_dispatch_at == t_old


class TestTickDecisionShape:
    # TickDecision is a frozen dataclass. This is a cheap regression guard —
    # the tick contract across tests relies on `campaign_id` and `is_retry`.

    def test_none_decision(self) -> None:
        d = TickDecision(campaign_id=None, is_retry=False)
        assert d.campaign_id is None
        assert d.is_retry is False

    def test_populated_decision(self) -> None:
        cid = UUID("00000000-0000-0000-0000-000000000001")
        d = TickDecision(campaign_id=cid, is_retry=True)
        assert d.campaign_id == cid
        assert d.is_retry is True

    def test_frozen(self) -> None:
        d = TickDecision(campaign_id=None, is_retry=False)
        with pytest.raises(AttributeError):
            d.campaign_id = UUID("00000000-0000-0000-0000-000000000001")  # type: ignore[misc]
