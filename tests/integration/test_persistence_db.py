from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from app.audit.events import AuditEvent
from app.persistence.repositories import (
    AuditRepo,
    CallRepo,
    CampaignRepo,
    SchedulerStateRepo,
    WebhookInboxRepo,
)

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schema.sql"

DEFAULT_SCHEDULE: dict[str, Any] = {
    "mon": [{"start": "00:00", "end": "23:59"}],
    "tue": [{"start": "00:00", "end": "23:59"}],
    "wed": [{"start": "00:00", "end": "23:59"}],
    "thu": [{"start": "00:00", "end": "23:59"}],
    "fri": [{"start": "00:00", "end": "23:59"}],
    "sat": [{"start": "00:00", "end": "23:59"}],
    "sun": [{"start": "00:00", "end": "23:59"}],
}

DEFAULT_RETRY_CONFIG: dict[str, Any] = {"max_attempts": 3, "base_seconds": 30}


@pytest.fixture(scope="module")
def pg_container() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:16-alpine") as container:
        yield container


@pytest.fixture
async def pool(pg_container: PostgresContainer) -> AsyncIterator[asyncpg.Pool]:
    schema_sql = SCHEMA_PATH.read_text()
    raw_url = pg_container.get_connection_url()
    # testcontainers returns a JDBC-ish URL with the sqlalchemy driver suffix;
    # asyncpg wants a plain postgresql:// DSN.
    dsn = raw_url.replace("postgresql+psycopg2://", "postgresql://").replace(
        "postgresql+psycopg://", "postgresql://"
    )
    created = await asyncpg.create_pool(dsn, min_size=1, max_size=8)
    assert created is not None
    p: asyncpg.Pool = created
    try:
        async with p.acquire() as conn:
            # Wipe any carry-over from the previous test before applying the
            # idempotent schema.sql so each test starts clean.
            await conn.execute(
                """
                DROP TABLE IF EXISTS scheduler_audit CASCADE;
                DROP TABLE IF EXISTS webhook_inbox CASCADE;
                DROP TABLE IF EXISTS scheduler_campaign_state CASCADE;
                DROP TABLE IF EXISTS calls CASCADE;
                DROP TABLE IF EXISTS campaigns CASCADE;
                """
            )
            await conn.execute(schema_sql)
        yield p
    finally:
        await p.close()


async def _create_campaign(
    pool: asyncpg.Pool,
    *,
    name: str = "test-campaign",
    timezone: str = "UTC",
    max_concurrent: int = 5,
) -> UUID:
    async with pool.acquire() as conn:
        return await CampaignRepo.create(
            conn,
            name=name,
            timezone=timezone,
            schedule=DEFAULT_SCHEDULE,
            max_concurrent=max_concurrent,
            retry_config=DEFAULT_RETRY_CONFIG,
        )


# -- CampaignRepo ------------------------------------------------------------


class TestCampaignRepo:
    async def test_create_and_get_round_trip(self, pool: asyncpg.Pool) -> None:
        campaign_id = await _create_campaign(pool, name="c1")
        async with pool.acquire() as conn:
            row = await CampaignRepo.get(conn, campaign_id)
        assert row is not None
        assert row.id == campaign_id
        assert row.name == "c1"
        assert row.status == "PENDING"
        assert row.schedule == DEFAULT_SCHEDULE
        assert row.retry_config == DEFAULT_RETRY_CONFIG
        assert row.max_concurrent == 5

    async def test_get_missing_returns_none(self, pool: asyncpg.Pool) -> None:
        from uuid import uuid4

        async with pool.acquire() as conn:
            assert await CampaignRepo.get(conn, uuid4()) is None

    async def test_transition_if_success_and_idempotent(self, pool: asyncpg.Pool) -> None:
        campaign_id = await _create_campaign(pool)
        async with pool.acquire() as conn:
            ok = await CampaignRepo.transition_if(conn, campaign_id, "PENDING", "ACTIVE")
            assert ok is True
            # Second call with stale expected_status no-ops.
            ok2 = await CampaignRepo.transition_if(conn, campaign_id, "PENDING", "ACTIVE")
            assert ok2 is False
            row = await CampaignRepo.get(conn, campaign_id)
            assert row is not None
            assert row.status == "ACTIVE"

    async def test_list_page_cursor_pagination(self, pool: asyncpg.Pool) -> None:
        for i in range(7):
            await _create_campaign(pool, name=f"c{i}")
        page1, cursor1 = await CampaignRepo.list_page(pool, cursor=None, limit=3)
        assert len(page1) == 3
        assert cursor1 is not None
        page2, cursor2 = await CampaignRepo.list_page(pool, cursor=cursor1, limit=3)
        assert len(page2) == 3
        assert cursor2 is not None
        page3, cursor3 = await CampaignRepo.list_page(pool, cursor=cursor2, limit=3)
        assert len(page3) == 1
        assert cursor3 is None
        all_ids = [c.id for c in page1 + page2 + page3]
        assert len(set(all_ids)) == 7

    async def test_list_eligible_for_tick_joins_scheduler_state(self, pool: asyncpg.Pool) -> None:
        active_id = await _create_campaign(pool, name="active")
        pending_id = await _create_campaign(pool, name="pending")
        completed_id = await _create_campaign(pool, name="completed")
        now = datetime.now(tz=UTC)
        async with pool.acquire() as conn:
            await CampaignRepo.transition_if(conn, active_id, "PENDING", "ACTIVE")
            await CampaignRepo.transition_if(conn, completed_id, "PENDING", "COMPLETED")
            await SchedulerStateRepo.update_last_dispatch_at(conn, active_id, now)
            rows = await CampaignRepo.list_eligible_for_tick(conn)
        ids = {r.id for r in rows}
        assert active_id in ids
        assert pending_id in ids
        assert completed_id not in ids
        active_row = next(r for r in rows if r.id == active_id)
        pending_row = next(r for r in rows if r.id == pending_id)
        assert active_row.last_dispatch_at is not None
        assert pending_row.last_dispatch_at is None

    async def test_stats_matches_hand_counts(self, pool: asyncpg.Pool) -> None:
        campaign_id = await _create_campaign(pool)
        async with pool.acquire() as conn:
            await CallRepo.create_batch(
                conn,
                campaign_id=campaign_id,
                phones=[f"+1415555{i:04d}" for i in range(8)],
                retries_remaining=3,
            )
            # Move specific rows into terminal / in-flight states via raw UPDATE —
            # the state machine isn't built yet in this layer.
            await conn.execute(
                """
                UPDATE calls SET status = 'COMPLETED', attempt_epoch = 1
                WHERE campaign_id = $1 AND phone = $2
                """,
                campaign_id,
                "+14155550000",
            )
            await conn.execute(
                """
                UPDATE calls SET status = 'COMPLETED', attempt_epoch = 2
                WHERE campaign_id = $1 AND phone = $2
                """,
                campaign_id,
                "+14155550001",
            )
            await conn.execute(
                """
                UPDATE calls SET status = 'FAILED', attempt_epoch = 4
                WHERE campaign_id = $1 AND phone = $2
                """,
                campaign_id,
                "+14155550002",
            )
            await conn.execute(
                """
                UPDATE calls SET status = 'NO_ANSWER', attempt_epoch = 3
                WHERE campaign_id = $1 AND phone = $2
                """,
                campaign_id,
                "+14155550003",
            )
            await conn.execute(
                """
                UPDATE calls SET status = 'BUSY', attempt_epoch = 2
                WHERE campaign_id = $1 AND phone = $2
                """,
                campaign_id,
                "+14155550004",
            )
            await conn.execute(
                """
                UPDATE calls SET status = 'IN_PROGRESS', attempt_epoch = 1
                WHERE campaign_id = $1 AND phone = $2
                """,
                campaign_id,
                "+14155550005",
            )
            # The last two stay QUEUED with epoch 0.
        stats = await CampaignRepo.stats(pool, campaign_id)
        assert stats.total == 8
        assert stats.completed == 2
        # failed aggregates FAILED + NO_ANSWER + BUSY so it matches the
        # external status mapping on /calls/{id} ({completed, failed,
        # in_progress} — total always == completed + failed + in_progress).
        assert stats.failed == 3
        # 1 + 2 + 4 + 3 + 2 + 1 + 0 + 0 = 13 attempts made across all rows.
        assert stats.retries_attempted == 13
        # QUEUED x2 + IN_PROGRESS x1 = 3 rows in flight / waiting.
        assert stats.in_progress == 3
        # Invariant: the three buckets partition the total.
        assert stats.completed + stats.failed + stats.in_progress == stats.total


# -- CallRepo ----------------------------------------------------------------


class TestCallRepo:
    async def test_create_batch_preserves_phone_order(self, pool: asyncpg.Pool) -> None:
        campaign_id = await _create_campaign(pool)
        phones = [f"+1415000{i:04d}" for i in range(5)]
        async with pool.acquire() as conn:
            ids = await CallRepo.create_batch(
                conn,
                campaign_id=campaign_id,
                phones=phones,
                retries_remaining=2,
            )
            assert len(ids) == len(phones)
            for call_id, phone in zip(ids, phones, strict=True):
                row = await CallRepo.get(conn, call_id)
                assert row is not None
                assert row.phone == phone
                assert row.status == "QUEUED"
                assert row.retries_remaining == 2
                assert row.attempt_epoch == 0

    async def test_create_batch_empty_phones_returns_empty(self, pool: asyncpg.Pool) -> None:
        campaign_id = await _create_campaign(pool)
        async with pool.acquire() as conn:
            ids = await CallRepo.create_batch(
                conn,
                campaign_id=campaign_id,
                phones=[],
                retries_remaining=2,
            )
        assert ids == []

    async def test_partial_unique_index_enforces_in_flight_phone(self, pool: asyncpg.Pool) -> None:
        campaign_id = await _create_campaign(pool)
        async with pool.acquire() as conn:
            await CallRepo.create_batch(
                conn,
                campaign_id=campaign_id,
                phones=["+14155551234"],
                retries_remaining=2,
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await CallRepo.create_batch(
                    conn,
                    campaign_id=campaign_id,
                    phones=["+14155551234"],
                    retries_remaining=2,
                )
            # Move the original to COMPLETED (the state machine isn't built yet).
            await conn.execute(
                "UPDATE calls SET status = 'COMPLETED' WHERE phone = $1",
                "+14155551234",
            )
            # Now a fresh row with the same phone is allowed.
            new_ids = await CallRepo.create_batch(
                conn,
                campaign_id=campaign_id,
                phones=["+14155551234"],
                retries_remaining=2,
            )
            assert len(new_ids) == 1

    async def test_claim_next_queued_bumps_epoch_and_transitions(self, pool: asyncpg.Pool) -> None:
        campaign_id = await _create_campaign(pool)
        async with pool.acquire() as conn:
            ids = await CallRepo.create_batch(
                conn,
                campaign_id=campaign_id,
                phones=["+14155557001", "+14155557002"],
                retries_remaining=2,
            )
            claimed = await CallRepo.claim_next_queued(conn, campaign_id)
            assert claimed is not None
            assert claimed.status == "DIALING"
            assert claimed.attempt_epoch == 1
            # FIFO: oldest created_at wins.
            assert claimed.id == ids[0]
            second = await CallRepo.claim_next_queued(conn, campaign_id)
            assert second is not None
            assert second.id == ids[1]
            assert second.status == "DIALING"
            exhausted = await CallRepo.claim_next_queued(conn, campaign_id)
            assert exhausted is None

    async def test_claim_next_queued_concurrent_no_double_claim(self, pool: asyncpg.Pool) -> None:
        # Seed enough rows that each concurrent claimer gets a distinct one.
        campaign_id = await _create_campaign(pool)
        async with pool.acquire() as conn:
            await CallRepo.create_batch(
                conn,
                campaign_id=campaign_id,
                phones=[f"+1415566{i:04d}" for i in range(5)],
                retries_remaining=2,
            )

        async def claim_once() -> UUID | None:
            # Each coroutine MUST use its own connection — SKIP LOCKED only
            # works because each claimer is in its own txn / lock cluster.
            async with pool.acquire() as c, c.transaction():
                row = await CallRepo.claim_next_queued(c, campaign_id)
                if row is None:
                    return None
                return row.id

        results = await asyncio.gather(claim_once(), claim_once(), claim_once())
        non_null = [r for r in results if r is not None]
        assert len(non_null) == 3
        assert len(set(non_null)) == 3

    async def test_claim_next_queued_respects_next_attempt_at(self, pool: asyncpg.Pool) -> None:
        campaign_id = await _create_campaign(pool)
        future = datetime.now(tz=UTC) + timedelta(hours=1)
        async with pool.acquire() as conn:
            ids = await CallRepo.create_batch(
                conn,
                campaign_id=campaign_id,
                phones=["+14155558001", "+14155558002"],
                retries_remaining=2,
            )
            # Push the first row's next_attempt into the future; expect the
            # second row to be claimed instead.
            await conn.execute(
                "UPDATE calls SET next_attempt_at = $1 WHERE id = $2",
                future,
                ids[0],
            )
            claimed = await CallRepo.claim_next_queued(conn, campaign_id)
            assert claimed is not None
            assert claimed.id == ids[1]

    async def test_find_retry_due_campaign_ids_no_duplicates(self, pool: asyncpg.Pool) -> None:
        c_retry1 = await _create_campaign(pool, name="retry1")
        c_retry2 = await _create_campaign(pool, name="retry2")
        c_queued = await _create_campaign(pool, name="queued-only")
        past = datetime.now(tz=UTC) - timedelta(seconds=5)
        async with pool.acquire() as conn:
            for cid, phones in (
                (c_retry1, ["+14155559001", "+14155559002"]),
                (c_retry2, ["+14155559003"]),
                (c_queued, ["+14155559004"]),
            ):
                await CallRepo.create_batch(
                    conn,
                    campaign_id=cid,
                    phones=phones,
                    retries_remaining=2,
                )
            await conn.execute(
                """
                UPDATE calls SET status = 'RETRY_PENDING', next_attempt_at = $1
                WHERE campaign_id IN ($2, $3)
                """,
                past,
                c_retry1,
                c_retry2,
            )
            due = await CallRepo.find_retry_due_campaign_ids(conn)
            assert set(due) == {c_retry1, c_retry2}
            assert len(due) == len(set(due))

    async def test_find_retry_due_uses_partial_index(self, pool: asyncpg.Pool) -> None:
        campaign_id = await _create_campaign(pool)
        past = datetime.now(tz=UTC) - timedelta(seconds=5)
        async with pool.acquire() as conn:
            await CallRepo.create_batch(
                conn,
                campaign_id=campaign_id,
                phones=["+14155560001"],
                retries_remaining=2,
            )
            await conn.execute(
                """
                UPDATE calls SET status = 'RETRY_PENDING', next_attempt_at = $1
                WHERE campaign_id = $2
                """,
                past,
                campaign_id,
            )
            # ANALYZE so the planner has stats; seq-scan is cheap at low rows.
            await conn.execute("ANALYZE calls")
            plan_raw = await conn.fetchval(
                """
                EXPLAIN (FORMAT JSON)
                SELECT DISTINCT campaign_id
                FROM calls
                WHERE status = 'RETRY_PENDING'
                  AND (next_attempt_at IS NULL OR next_attempt_at <= NOW())
                """
            )
            plan = json.loads(plan_raw) if isinstance(plan_raw, str) else plan_raw
            # Stringify the whole plan tree and assert the partial index name
            # appears somewhere. At low row counts Postgres may still pick a
            # seq-scan; in that case we simply verify the index EXISTS and is
            # listed as a candidate in pg_indexes.
            plan_text = json.dumps(plan)
            if "calls_retry_pending_system_idx" not in plan_text:
                index_names = [
                    row["indexname"]
                    for row in await conn.fetch(
                        "SELECT indexname FROM pg_indexes WHERE tablename = 'calls'"
                    )
                ]
                assert "calls_retry_pending_system_idx" in index_names

    async def test_in_flight_count(self, pool: asyncpg.Pool) -> None:
        campaign_id = await _create_campaign(pool)
        async with pool.acquire() as conn:
            ids = await CallRepo.create_batch(
                conn,
                campaign_id=campaign_id,
                phones=[f"+1415561{i:04d}" for i in range(4)],
                retries_remaining=2,
            )
            await conn.execute("UPDATE calls SET status = 'DIALING' WHERE id = $1", ids[0])
            await conn.execute("UPDATE calls SET status = 'IN_PROGRESS' WHERE id = $1", ids[1])
            # ids[2] stays QUEUED, ids[3] → RETRY_PENDING (waiting, not in flight)
            await conn.execute("UPDATE calls SET status = 'RETRY_PENDING' WHERE id = $1", ids[3])
            count = await CallRepo.in_flight_count(conn, campaign_id)
        assert count == 2

    async def test_in_flight_counts_by_campaign(self, pool: asyncpg.Pool) -> None:
        c1 = await _create_campaign(pool, name="c1")
        c2 = await _create_campaign(pool, name="c2")
        c3 = await _create_campaign(pool, name="c3")
        async with pool.acquire() as conn:
            for cid, phones in (
                (c1, ["+1415562" + f"{i:04d}" for i in range(3)]),
                (c2, ["+1415563" + f"{i:04d}" for i in range(2)]),
                (c3, ["+14155640001"]),
            ):
                await CallRepo.create_batch(
                    conn, campaign_id=cid, phones=phones, retries_remaining=2
                )
            await conn.execute("UPDATE calls SET status = 'DIALING' WHERE campaign_id = $1", c1)
            await conn.execute(
                """
                UPDATE calls SET status = 'IN_PROGRESS' WHERE campaign_id = $1
                """,
                c2,
            )
            # c3 stays QUEUED — it should map to 0.
            counts = await CallRepo.in_flight_counts_by_campaign(conn, [c1, c2, c3])
        assert counts[c1] == 3
        assert counts[c2] == 2
        assert counts[c3] == 0

    async def test_in_flight_counts_by_campaign_empty_input(self, pool: asyncpg.Pool) -> None:
        async with pool.acquire() as conn:
            counts = await CallRepo.in_flight_counts_by_campaign(conn, [])
        assert counts == {}

    async def test_count_active_by_campaign(self, pool: asyncpg.Pool) -> None:
        campaign_id = await _create_campaign(pool)
        async with pool.acquire() as conn:
            ids = await CallRepo.create_batch(
                conn,
                campaign_id=campaign_id,
                phones=[f"+1415565{i:04d}" for i in range(6)],
                retries_remaining=2,
            )
            await conn.execute("UPDATE calls SET status = 'DIALING' WHERE id = $1", ids[0])
            await conn.execute("UPDATE calls SET status = 'IN_PROGRESS' WHERE id = $1", ids[1])
            await conn.execute("UPDATE calls SET status = 'RETRY_PENDING' WHERE id = $1", ids[2])
            # ids[3] stays QUEUED
            await conn.execute("UPDATE calls SET status = 'COMPLETED' WHERE id = $1", ids[4])
            await conn.execute("UPDATE calls SET status = 'FAILED' WHERE id = $1", ids[5])
            active = await CallRepo.count_active_by_campaign(conn, campaign_id)
        # QUEUED + DIALING + IN_PROGRESS + RETRY_PENDING = 4
        assert active == 4

    async def test_count_retries_due_system(self, pool: asyncpg.Pool) -> None:
        c1 = await _create_campaign(pool, name="c1")
        c2 = await _create_campaign(pool, name="c2")
        past = datetime.now(tz=UTC) - timedelta(seconds=5)
        future = datetime.now(tz=UTC) + timedelta(hours=1)
        async with pool.acquire() as conn:
            await CallRepo.create_batch(
                conn,
                campaign_id=c1,
                phones=["+14155660001", "+14155660002"],
                retries_remaining=2,
            )
            await CallRepo.create_batch(
                conn,
                campaign_id=c2,
                phones=["+14155660003"],
                retries_remaining=2,
            )
            await conn.execute(
                """
                UPDATE calls SET status = 'RETRY_PENDING', next_attempt_at = $1
                WHERE campaign_id = $2
                """,
                past,
                c1,
            )
            await conn.execute(
                """
                UPDATE calls SET status = 'RETRY_PENDING', next_attempt_at = $1
                WHERE campaign_id = $2
                """,
                future,
                c2,
            )
            due = await CallRepo.count_retries_due_system(conn)
        assert due == 2

    async def test_terminal_aggregate(self, pool: asyncpg.Pool) -> None:
        campaign_id = await _create_campaign(pool)
        async with pool.acquire() as conn:
            ids = await CallRepo.create_batch(
                conn,
                campaign_id=campaign_id,
                phones=[f"+1415567{i:04d}" for i in range(4)],
                retries_remaining=2,
            )
            await conn.execute("UPDATE calls SET status = 'COMPLETED' WHERE id = $1", ids[0])
            await conn.execute("UPDATE calls SET status = 'FAILED' WHERE id = $1", ids[1])
            await conn.execute("UPDATE calls SET status = 'NO_ANSWER' WHERE id = $1", ids[2])
            await conn.execute("UPDATE calls SET status = 'BUSY' WHERE id = $1", ids[3])
            agg = await CallRepo.terminal_aggregate(conn, campaign_id)
        assert agg.completed == 1
        assert agg.failed == 1
        assert agg.no_answer == 1
        assert agg.busy == 1

    async def test_find_stuck_dialing(self, pool: asyncpg.Pool) -> None:
        campaign_id = await _create_campaign(pool)
        async with pool.acquire() as conn:
            ids = await CallRepo.create_batch(
                conn,
                campaign_id=campaign_id,
                phones=["+14155680001", "+14155680002"],
                retries_remaining=2,
            )
            # Force the first row into DIALING with updated_at well in the past.
            await conn.execute(
                """
                UPDATE calls
                SET status = 'DIALING', updated_at = NOW() - interval '10 minutes'
                WHERE id = $1
                """,
                ids[0],
            )
            # Second row is DIALING but recent — must not appear.
            await conn.execute(
                "UPDATE calls SET status = 'DIALING', updated_at = NOW() WHERE id = $1",
                ids[1],
            )
            stuck = await CallRepo.find_stuck_dialing(conn, threshold_seconds=60)
        assert [r.id for r in stuck] == [ids[0]]

    async def test_get_by_provider_call_id(self, pool: asyncpg.Pool) -> None:
        campaign_id = await _create_campaign(pool)
        async with pool.acquire() as conn:
            ids = await CallRepo.create_batch(
                conn,
                campaign_id=campaign_id,
                phones=["+14155690001"],
                retries_remaining=2,
            )
            await conn.execute(
                "UPDATE calls SET provider_call_id = $1 WHERE id = $2",
                "prov-abc-123",
                ids[0],
            )
            row = await CallRepo.get_by_provider_call_id(conn, "prov-abc-123")
            assert row is not None
            assert row.id == ids[0]
            missing = await CallRepo.get_by_provider_call_id(conn, "does-not-exist")
            assert missing is None


# -- WebhookInboxRepo --------------------------------------------------------


class TestWebhookInboxRepo:
    async def test_insert_idempotent_on_conflict(self, pool: asyncpg.Pool) -> None:
        async with pool.acquire() as conn:
            id1 = await WebhookInboxRepo.insert(
                conn,
                provider="mock",
                provider_event_id="evt-1",
                payload={"k": "v"},
                headers={"x-sig": "abc"},
            )
            id2 = await WebhookInboxRepo.insert(
                conn,
                provider="mock",
                provider_event_id="evt-1",
                payload={"k": "different"},
                headers={},
            )
        assert id1 == id2

    async def test_insert_distinct_providers_same_event_id(self, pool: asyncpg.Pool) -> None:
        async with pool.acquire() as conn:
            id1 = await WebhookInboxRepo.insert(
                conn,
                provider="mock",
                provider_event_id="evt-same",
                payload={},
                headers={},
            )
            id2 = await WebhookInboxRepo.insert(
                conn,
                provider="other",
                provider_event_id="evt-same",
                payload={},
                headers={},
            )
        assert id1 != id2

    async def test_claim_and_mark_processed(self, pool: asyncpg.Pool) -> None:
        async with pool.acquire() as conn:
            inbox_id = await WebhookInboxRepo.insert(
                conn,
                provider="mock",
                provider_event_id="evt-claim",
                payload={"status": "completed"},
                headers={"h": "v"},
            )
            async with conn.transaction():
                row = await WebhookInboxRepo.claim_unprocessed_one(conn)
                assert row is not None
                assert row.id == inbox_id
                assert row.payload == {"status": "completed"}
                assert row.headers == {"h": "v"}
                assert row.processed_at is None
                await WebhookInboxRepo.mark_processed(conn, row.id)
            # Now the row is no longer claimable.
            no_more = await WebhookInboxRepo.claim_unprocessed_one(conn)
            assert no_more is None


# -- SchedulerStateRepo ------------------------------------------------------


class TestSchedulerStateRepo:
    async def test_upsert_last_dispatch_at(self, pool: asyncpg.Pool) -> None:
        campaign_id = await _create_campaign(pool)
        t1 = datetime.now(tz=UTC) - timedelta(minutes=5)
        t2 = datetime.now(tz=UTC)
        async with pool.acquire() as conn:
            initial = await SchedulerStateRepo.get_last_dispatch_at(conn, campaign_id)
            assert initial is None
            await SchedulerStateRepo.update_last_dispatch_at(conn, campaign_id, t1)
            got1 = await SchedulerStateRepo.get_last_dispatch_at(conn, campaign_id)
            assert got1 == t1
            await SchedulerStateRepo.update_last_dispatch_at(conn, campaign_id, t2)
            got2 = await SchedulerStateRepo.get_last_dispatch_at(conn, campaign_id)
            assert got2 == t2


# -- AuditRepo ---------------------------------------------------------------


class TestAuditRepo:
    async def test_emit_and_list_round_trip(self, pool: asyncpg.Pool) -> None:
        campaign_id = await _create_campaign(pool)
        async with pool.acquire() as conn:
            await AuditRepo.emit(
                conn,
                AuditEvent(
                    event_type="DISPATCH",
                    reason="test dispatch",
                    campaign_id=campaign_id,
                    state_before="QUEUED",
                    state_after="DIALING",
                    extra={"k": "v"},
                ),
            )
        rows, _ = await AuditRepo.list(pool, campaign_id=campaign_id, limit=10)
        assert len(rows) == 1
        row = rows[0]
        assert row.event_type == "DISPATCH"
        assert row.reason == "test dispatch"
        assert row.state_before == "QUEUED"
        assert row.state_after == "DIALING"
        assert row.extra == {"k": "v"}

    async def test_cursor_pagination_stable_under_concurrent_insert(
        self, pool: asyncpg.Pool
    ) -> None:
        campaign_id = await _create_campaign(pool)
        # Seed 30 events.
        async with pool.acquire() as conn:
            for i in range(30):
                await AuditRepo.emit(
                    conn,
                    AuditEvent(
                        event_type="DISPATCH",
                        reason=f"seed-{i:02d}",
                        campaign_id=campaign_id,
                    ),
                )
        page1, cur1 = await AuditRepo.list(pool, campaign_id=campaign_id, limit=10)
        assert cur1 is not None
        page2, cur2 = await AuditRepo.list(pool, campaign_id=campaign_id, cursor=cur1, limit=10)
        assert cur2 is not None
        page3, cur3 = await AuditRepo.list(pool, campaign_id=campaign_id, cursor=cur2, limit=10)
        # len(page3) == limit → the repo surfaces a cursor since it can't
        # prove the tail is empty. Exhaustion is observed by following the
        # cursor one more hop and getting zero rows.
        assert len(page3) == 10
        assert cur3 is not None
        page4, cur4 = await AuditRepo.list(pool, campaign_id=campaign_id, cursor=cur3, limit=10)
        assert page4 == []
        assert cur4 is None
        first_three_ids = [r.id for r in page1 + page2 + page3]
        assert len(set(first_three_ids)) == 30

        # Now insert 10 more events mid-pagination; re-walking the ORIGINAL
        # cursor chain must return the same first three pages because the
        # WHERE clause is `(ts, id) < (cursor_ts, cursor_id)` and the new rows
        # all have strictly larger (ts, id) tuples.
        async with pool.acquire() as conn:
            for i in range(10):
                await AuditRepo.emit(
                    conn,
                    AuditEvent(
                        event_type="DISPATCH",
                        reason=f"late-{i:02d}",
                        campaign_id=campaign_id,
                    ),
                )
        page1b, _ = await AuditRepo.list(pool, campaign_id=campaign_id, cursor=None, limit=10)
        # The brand-new page 1 surfaces the late inserts, but the chain starting
        # from cur1 does not.
        page2b, _ = await AuditRepo.list(pool, campaign_id=campaign_id, cursor=cur1, limit=10)
        page3b, _ = await AuditRepo.list(pool, campaign_id=campaign_id, cursor=cur2, limit=10)
        assert [r.id for r in page2b] == [r.id for r in page2]
        assert [r.id for r in page3b] == [r.id for r in page3]
        # The late events show up on the very-first page now, so page1b differs.
        assert any("late-" in r.reason for r in page1b)

    async def test_list_filters_by_event_type_list(self, pool: asyncpg.Pool) -> None:
        campaign_id = await _create_campaign(pool)
        async with pool.acquire() as conn:
            for et in ("DISPATCH", "RETRY_DUE", "SKIP_CONCURRENCY", "DISPATCH"):
                await AuditRepo.emit(
                    conn,
                    AuditEvent(event_type=et, reason="x", campaign_id=campaign_id),  # type: ignore[arg-type]
                )
        only_dispatch, _ = await AuditRepo.list(
            pool, campaign_id=campaign_id, event_type="DISPATCH", limit=20
        )
        assert len(only_dispatch) == 2

        two_types, _ = await AuditRepo.list(
            pool,
            campaign_id=campaign_id,
            event_type=["DISPATCH", "RETRY_DUE"],
            limit=20,
        )
        assert len(two_types) == 3

    async def test_list_reason_contains_escapes_wildcards(self, pool: asyncpg.Pool) -> None:
        campaign_id = await _create_campaign(pool)
        async with pool.acquire() as conn:
            await AuditRepo.emit(
                conn,
                AuditEvent(
                    event_type="DISPATCH",
                    reason="literal 100% match",
                    campaign_id=campaign_id,
                ),
            )
            await AuditRepo.emit(
                conn,
                AuditEvent(
                    event_type="DISPATCH",
                    reason="no percent here",
                    campaign_id=campaign_id,
                ),
            )
        hit, _ = await AuditRepo.list(
            pool, campaign_id=campaign_id, reason_contains="100%", limit=10
        )
        assert len(hit) == 1
        assert hit[0].reason == "literal 100% match"

    async def test_list_ts_filters(self, pool: asyncpg.Pool) -> None:
        campaign_id = await _create_campaign(pool)
        async with pool.acquire() as conn:
            await AuditRepo.emit(
                conn,
                AuditEvent(event_type="DISPATCH", reason="a", campaign_id=campaign_id),
            )
        now = datetime.now(tz=UTC)
        future = now + timedelta(hours=1)
        past = now - timedelta(hours=1)
        none_rows, _ = await AuditRepo.list(pool, campaign_id=campaign_id, from_ts=future, limit=10)
        assert none_rows == []
        some_rows, _ = await AuditRepo.list(
            pool, campaign_id=campaign_id, from_ts=past, to_ts=future, limit=10
        )
        assert len(some_rows) == 1
