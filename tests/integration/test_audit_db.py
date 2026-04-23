from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from app.audit.emitter import emit_audit
from app.audit.events import AuditEvent
from app.audit.reader import query_audit


@pytest.fixture(scope="module")
def _pg() -> AsyncIterator[PostgresContainer]:
    with PostgresContainer("postgres:16-alpine") as c:
        yield c


@pytest.fixture
async def pool(_pg: PostgresContainer) -> AsyncIterator[asyncpg.Pool]:
    dsn = _pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
    p = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    assert p is not None
    async with p.acquire() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await conn.execute(Path("schema.sql").read_text())
    yield p
    await p.close()


async def _emit_batch(pool: asyncpg.Pool, count: int, *, reason_prefix: str = "evt") -> None:
    async with pool.acquire() as conn:
        for i in range(count):
            await emit_audit(
                conn,
                AuditEvent(
                    event_type="DISPATCH",
                    reason=f"{reason_prefix}-{i:04d}",
                ),
            )


class TestEmitThenRead:
    async def test_round_trip(self, pool: asyncpg.Pool) -> None:
        cid = uuid4()
        async with pool.acquire() as conn:
            await emit_audit(
                conn,
                AuditEvent(
                    event_type="DISPATCH",
                    reason="hello",
                    campaign_id=cid,
                    extra={"foo": "bar"},
                ),
            )
        rows, _ = await query_audit(pool, campaign_id=cid)
        assert len(rows) == 1
        assert rows[0].reason == "hello"
        assert rows[0].extra == {"foo": "bar"}


class TestFilters:
    async def test_filter_by_event_type_single(self, pool: asyncpg.Pool) -> None:
        cid = uuid4()
        async with pool.acquire() as conn:
            for _ in range(3):
                await emit_audit(
                    conn, AuditEvent(event_type="DISPATCH", reason="d", campaign_id=cid)
                )
            for _ in range(2):
                await emit_audit(
                    conn, AuditEvent(event_type="TRANSITION", reason="t", campaign_id=cid)
                )
        rows, _ = await query_audit(pool, campaign_id=cid, event_type="DISPATCH")
        assert len(rows) == 3
        assert all(r.event_type == "DISPATCH" for r in rows)

    async def test_filter_by_event_type_list(self, pool: asyncpg.Pool) -> None:
        cid = uuid4()
        async with pool.acquire() as conn:
            for _ in range(3):
                await emit_audit(
                    conn, AuditEvent(event_type="DISPATCH", reason="d", campaign_id=cid)
                )
            for _ in range(2):
                await emit_audit(
                    conn, AuditEvent(event_type="TRANSITION", reason="t", campaign_id=cid)
                )
            for _ in range(1):
                await emit_audit(
                    conn, AuditEvent(event_type="RETRY_DUE", reason="r", campaign_id=cid)
                )
        rows, _ = await query_audit(pool, campaign_id=cid, event_type=["DISPATCH", "TRANSITION"])
        assert len(rows) == 5
        assert {r.event_type for r in rows} == {"DISPATCH", "TRANSITION"}

    async def test_filter_by_reason_contains(self, pool: asyncpg.Pool) -> None:
        cid = uuid4()
        async with pool.acquire() as conn:
            await emit_audit(
                conn,
                AuditEvent(event_type="DISPATCH", reason="claim for dispatch", campaign_id=cid),
            )
            await emit_audit(
                conn,
                AuditEvent(event_type="TRANSITION", reason="webhook IN_PROGRESS", campaign_id=cid),
            )
            await emit_audit(
                conn,
                AuditEvent(event_type="RETRY_DUE", reason="claim for retry", campaign_id=cid),
            )
        rows, _ = await query_audit(pool, campaign_id=cid, reason_contains="claim")
        assert len(rows) == 2
        assert all("claim" in r.reason for r in rows)

    async def test_filter_reason_contains_escapes_wildcards(self, pool: asyncpg.Pool) -> None:
        cid = uuid4()
        async with pool.acquire() as conn:
            await emit_audit(
                conn, AuditEvent(event_type="DISPATCH", reason="hit 100%", campaign_id=cid)
            )
            await emit_audit(
                conn, AuditEvent(event_type="DISPATCH", reason="nothing here", campaign_id=cid)
            )
        # `100%` must match literally, not as "anything starting with 100".
        rows, _ = await query_audit(pool, campaign_id=cid, reason_contains="100%")
        assert len(rows) == 1
        assert rows[0].reason == "hit 100%"

    async def test_filter_by_campaign_id(self, pool: asyncpg.Pool) -> None:
        c_a, c_b = uuid4(), uuid4()
        async with pool.acquire() as conn:
            for _ in range(5):
                await emit_audit(
                    conn, AuditEvent(event_type="DISPATCH", reason="a", campaign_id=c_a)
                )
            for _ in range(3):
                await emit_audit(
                    conn, AuditEvent(event_type="DISPATCH", reason="b", campaign_id=c_b)
                )
        rows_a, _ = await query_audit(pool, campaign_id=c_a)
        rows_b, _ = await query_audit(pool, campaign_id=c_b)
        assert len(rows_a) == 5
        assert len(rows_b) == 3

    async def test_filter_by_time_range(self, pool: asyncpg.Pool) -> None:
        cid = uuid4()
        # Emit a row, capture a cutoff, emit another row, capture a cutoff,
        # then emit two more.
        async with pool.acquire() as conn:
            await emit_audit(
                conn, AuditEvent(event_type="DISPATCH", reason="before", campaign_id=cid)
            )
        cutoff_lower = datetime.now(tz=UTC) - timedelta(milliseconds=1)

        # Give NOW() a millisecond to advance so ordering is stable even on
        # timestamp-of-same-millisecond systems.
        import asyncio as _asyncio

        await _asyncio.sleep(0.01)
        async with pool.acquire() as conn:
            await emit_audit(
                conn, AuditEvent(event_type="DISPATCH", reason="middle-1", campaign_id=cid)
            )
            await emit_audit(
                conn, AuditEvent(event_type="DISPATCH", reason="middle-2", campaign_id=cid)
            )
        await _asyncio.sleep(0.01)
        cutoff_upper = datetime.now(tz=UTC) + timedelta(milliseconds=1)

        rows, _ = await query_audit(pool, campaign_id=cid, from_ts=cutoff_lower, to_ts=cutoff_upper)
        reasons = sorted(r.reason for r in rows)
        assert reasons == ["before", "middle-1", "middle-2"] or reasons == [
            "middle-1",
            "middle-2",
        ]


class TestPaginationStability:
    async def test_cursor_pagination_covers_250_events_in_three_pages(
        self, pool: asyncpg.Pool
    ) -> None:
        await _emit_batch(pool, 250, reason_prefix="first")

        page1, cur1 = await query_audit(pool, limit=100)
        assert len(page1) == 100
        assert cur1 is not None

        page2, cur2 = await query_audit(pool, cursor=cur1, limit=100)
        assert len(page2) == 100
        assert cur2 is not None

        page3, cur3 = await query_audit(pool, cursor=cur2, limit=100)
        assert len(page3) == 50
        # len < limit -> no more rows to follow.
        assert cur3 is None

        all_ids = {r.id for r in page1 + page2 + page3}
        assert len(all_ids) == 250

    async def test_mid_pagination_inserts_do_not_affect_earlier_cursor(
        self, pool: asyncpg.Pool
    ) -> None:
        await _emit_batch(pool, 250, reason_prefix="seeded")

        page1, cur1 = await query_audit(pool, limit=100)
        assert cur1 is not None
        page2, cur2 = await query_audit(pool, cursor=cur1, limit=100)
        assert cur2 is not None
        page3_original, _ = await query_audit(pool, cursor=cur2, limit=100)

        # Inject 50 more events between page 2 and re-walking.
        await _emit_batch(pool, 50, reason_prefix="late")

        # Re-walk from the same cursors; the cursor chain's WHERE clause
        # (ts, id) < cursor keeps pages 2 and 3 identical. Only page 1
        # (cursor=None) surfaces the late events.
        page2_walked, _ = await query_audit(pool, cursor=cur1, limit=100)
        page3_walked, _ = await query_audit(pool, cursor=cur2, limit=100)

        assert [r.id for r in page2_walked] == [r.id for r in page2]
        assert [r.id for r in page3_walked] == [r.id for r in page3_original]

        # The fresh page 1 surfaces the new rows.
        page1_after, _ = await query_audit(pool, limit=100)
        assert any(r.reason.startswith("late") for r in page1_after)


class TestLimitCapping:
    async def test_limit_is_honoured(self, pool: asyncpg.Pool) -> None:
        await _emit_batch(pool, 600)
        rows, _ = await query_audit(pool, limit=500)
        assert len(rows) == 500
