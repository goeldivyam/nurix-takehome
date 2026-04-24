from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from app.state.campaign_terminal import maybe_promote_to_active
from app.state.machine import TransitionResult, transition
from app.state.types import CallStatus


@pytest.fixture(scope="module")
def _pg() -> AsyncIterator[PostgresContainer]:
    with PostgresContainer("postgres:16-alpine") as c:
        yield c


@pytest.fixture
async def pool(_pg: PostgresContainer) -> AsyncIterator[asyncpg.Pool]:
    dsn = _pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
    p = await asyncpg.create_pool(dsn, min_size=1, max_size=8)
    assert p is not None
    # Fresh schema per test for isolation — drop + recreate public schema and
    # re-apply schema.sql. Much simpler than per-test truncation of 5 tables.
    async with p.acquire() as conn:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        await conn.execute(Path("schema.sql").read_text())
    yield p
    await p.close()


async def _seed_campaign(
    pool: asyncpg.Pool,
    *,
    status: str = "PENDING",
    name: str = "campaign",
) -> UUID:
    async with pool.acquire() as conn:
        return await conn.fetchval(  # type: ignore[no-any-return]
            """
            INSERT INTO campaigns (name, status, timezone, schedule, max_concurrent, retry_config)
            VALUES ($1, $2, 'UTC', '{}'::jsonb, 3, '{}'::jsonb)
            RETURNING id
            """,
            name,
            status,
        )


async def _seed_call(
    pool: asyncpg.Pool,
    campaign_id: UUID,
    *,
    phone: str,
    status: str = "QUEUED",
    attempt_epoch: int = 0,
    retries_remaining: int = 2,
) -> UUID:
    async with pool.acquire() as conn:
        return await conn.fetchval(  # type: ignore[no-any-return]
            """
            INSERT INTO calls (campaign_id, phone, status, attempt_epoch, retries_remaining)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            campaign_id,
            phone,
            status,
            attempt_epoch,
            retries_remaining,
        )


async def _audit_rows(pool: asyncpg.Pool, **filters: object) -> list[dict[str, object]]:
    clauses = []
    params: list[object] = []
    for key, val in filters.items():
        params.append(val)
        clauses.append(f"{key} = ${len(params)}")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT event_type, campaign_id, call_id, state_before, "  # noqa: S608
            "state_after, extra, reason "
            f"FROM scheduler_audit {where} ORDER BY id",
            *params,
        )
    return [dict(r) for r in rows]


async def _campaign_status(pool: asyncpg.Pool, campaign_id: UUID) -> str:
    async with pool.acquire() as conn:
        return await conn.fetchval(  # type: ignore[no-any-return]
            "SELECT status FROM campaigns WHERE id = $1", campaign_id
        )


class TestTransitionCAS:
    async def test_queued_to_dialing_bumps_epoch_and_emits_audit(self, pool: asyncpg.Pool) -> None:
        campaign_id = await _seed_campaign(pool)
        call_id = await _seed_call(pool, campaign_id, phone="+14155550001")

        async with pool.acquire() as conn, conn.transaction():
            result = await transition(
                conn,
                call_id=call_id,
                expected_status=CallStatus.QUEUED,
                new_status=CallStatus.DIALING,
                expected_epoch=0,
                new_epoch=1,
                event_type="CLAIMED",
                reason="claim for dispatch",
            )

        assert result.applied
        assert result.row is not None
        assert result.row["status"] == "DIALING"
        assert result.row["attempt_epoch"] == 1

        rows = await _audit_rows(pool, call_id=call_id)
        # One CLAIMED for the call transition, one CAMPAIGN_PROMOTED_ACTIVE at campaign level.
        call_audits = [r for r in rows if r["event_type"] == "CLAIMED"]
        assert len(call_audits) == 1
        assert call_audits[0]["state_before"] == "QUEUED"
        assert call_audits[0]["state_after"] == "DIALING"

    async def test_stale_cas_returns_no_op_without_audit(self, pool: asyncpg.Pool) -> None:
        campaign_id = await _seed_campaign(pool, status="ACTIVE")
        call_id = await _seed_call(
            pool, campaign_id, phone="+14155550002", status="DIALING", attempt_epoch=1
        )

        async with pool.acquire() as conn, conn.transaction():
            # expected_epoch mismatches current value (1) -> no rows updated.
            result = await transition(
                conn,
                call_id=call_id,
                expected_status=CallStatus.DIALING,
                new_status=CallStatus.COMPLETED,
                expected_epoch=0,
                event_type="TRANSITION",
                reason="stale",
            )

        assert result.is_no_op()
        rows = await _audit_rows(pool, call_id=call_id)
        assert rows == []

    async def test_same_status_column_update_still_emits_audit(self, pool: asyncpg.Pool) -> None:
        campaign_id = await _seed_campaign(pool, status="ACTIVE")
        call_id = await _seed_call(
            pool, campaign_id, phone="+14155550003", status="DIALING", attempt_epoch=1
        )

        async with pool.acquire() as conn, conn.transaction():
            result = await transition(
                conn,
                call_id=call_id,
                expected_status=CallStatus.DIALING,
                new_status=CallStatus.DIALING,
                expected_epoch=1,
                event_type="DISPATCH",
                reason="dispatched to provider",
                column_updates={"provider_call_id": "pc-1"},
            )

        assert result.applied
        assert result.row is not None
        assert result.row["provider_call_id"] == "pc-1"
        assert result.row["attempt_epoch"] == 1

        rows = await _audit_rows(pool, call_id=call_id)
        dispatch_rows = [r for r in rows if r["event_type"] == "DISPATCH"]
        assert len(dispatch_rows) == 1

    async def test_unauthorized_column_update_raises_before_update(
        self, pool: asyncpg.Pool
    ) -> None:
        campaign_id = await _seed_campaign(pool, status="ACTIVE")
        call_id = await _seed_call(
            pool, campaign_id, phone="+14155550004", status="DIALING", attempt_epoch=1
        )

        async with pool.acquire() as conn, conn.transaction():
            with pytest.raises(ValueError, match="unauthorized column update"):
                await transition(
                    conn,
                    call_id=call_id,
                    expected_status=CallStatus.DIALING,
                    new_status=CallStatus.DIALING,
                    expected_epoch=1,
                    event_type="TRANSITION",
                    reason="test",
                    column_updates={"phone": "hacked"},
                )


class TestCampaignPromotion:
    async def test_first_queued_to_dialing_promotes_campaign_atomic(
        self, pool: asyncpg.Pool
    ) -> None:
        # Mirrors the scheduler's Phase 1 contract: claim-equivalent
        # transition + `maybe_promote_to_active` share ONE transaction on
        # ONE connection, so a crash between commits can't leave the call
        # DIALING under a PENDING campaign.
        campaign_id = await _seed_campaign(pool, status="PENDING")
        call_id = await _seed_call(pool, campaign_id, phone="+14155551001")

        async with pool.acquire() as conn, conn.transaction():
            result = await transition(
                conn,
                call_id=call_id,
                expected_status=CallStatus.QUEUED,
                new_status=CallStatus.DIALING,
                expected_epoch=0,
                new_epoch=1,
                event_type="CLAIMED",
                reason="claim",
            )
            assert result.applied
            await maybe_promote_to_active(conn, campaign_id)

        # Campaign was PENDING, now ACTIVE.
        assert await _campaign_status(pool, campaign_id) == "ACTIVE"
        rows = await _audit_rows(pool, campaign_id=campaign_id)
        events = {r["event_type"] for r in rows}
        assert "CAMPAIGN_PROMOTED_ACTIVE" in events
        assert "CLAIMED" in events

    async def test_promotion_rolled_back_with_transition(self, pool: asyncpg.Pool) -> None:
        campaign_id = await _seed_campaign(pool, status="PENDING")
        call_id = await _seed_call(pool, campaign_id, phone="+14155551002")

        # Open a transaction, run the transition + promotion, then raise to
        # force rollback. Mirrors tick's Phase 1 — both land or neither does.
        with pytest.raises(RuntimeError, match="forced rollback"):  # noqa: PT012
            async with pool.acquire() as conn, conn.transaction():
                await transition(
                    conn,
                    call_id=call_id,
                    expected_status=CallStatus.QUEUED,
                    new_status=CallStatus.DIALING,
                    expected_epoch=0,
                    new_epoch=1,
                    event_type="CLAIMED",
                    reason="claim",
                )
                await maybe_promote_to_active(conn, campaign_id)
                raise RuntimeError("forced rollback")

        # Neither row persisted — both the call transition AND the campaign
        # promotion are in the same transaction.
        async with pool.acquire() as conn:
            call_row = await conn.fetchrow("SELECT status FROM calls WHERE id = $1", call_id)
            assert call_row is not None
            assert call_row["status"] == "QUEUED"
        assert await _campaign_status(pool, campaign_id) == "PENDING"
        assert await _audit_rows(pool, campaign_id=campaign_id) == []


class TestTerminalRollup:
    async def test_terminal_rollup_to_completed(self, pool: asyncpg.Pool) -> None:
        campaign_id = await _seed_campaign(pool, status="ACTIVE")
        call_id = await _seed_call(
            pool, campaign_id, phone="+14155552001", status="IN_PROGRESS", attempt_epoch=1
        )

        async with pool.acquire() as conn, conn.transaction():
            result = await transition(
                conn,
                call_id=call_id,
                expected_status=CallStatus.IN_PROGRESS,
                new_status=CallStatus.COMPLETED,
                expected_epoch=1,
                event_type="TRANSITION",
                reason="provider completed",
            )
            assert result.applied

        assert await _campaign_status(pool, campaign_id) == "COMPLETED"
        rows = await _audit_rows(pool, campaign_id=campaign_id)
        rollup = [r for r in rows if r["event_type"] == "CAMPAIGN_COMPLETED"]
        assert len(rollup) == 1
        extra = (
            json.loads(rollup[0]["extra"])
            if isinstance(rollup[0]["extra"], str)
            else dict(rollup[0]["extra"])
        )
        assert extra["terminal"] == "COMPLETED"
        assert extra["completed"] == 1

    async def test_terminal_rollup_all_failed(self, pool: asyncpg.Pool) -> None:
        campaign_id = await _seed_campaign(pool, status="ACTIVE")
        call_a = await _seed_call(
            pool, campaign_id, phone="+14155552010", status="DIALING", attempt_epoch=1
        )
        call_b = await _seed_call(
            pool, campaign_id, phone="+14155552011", status="DIALING", attempt_epoch=1
        )

        async with pool.acquire() as conn, conn.transaction():
            await transition(
                conn,
                call_id=call_a,
                expected_status=CallStatus.DIALING,
                new_status=CallStatus.FAILED,
                expected_epoch=1,
                event_type="TRANSITION",
                reason="fail a",
            )
        # Campaign still ACTIVE after first failure (call_b still in flight).
        assert await _campaign_status(pool, campaign_id) == "ACTIVE"

        async with pool.acquire() as conn, conn.transaction():
            await transition(
                conn,
                call_id=call_b,
                expected_status=CallStatus.DIALING,
                new_status=CallStatus.FAILED,
                expected_epoch=1,
                event_type="TRANSITION",
                reason="fail b",
            )

        assert await _campaign_status(pool, campaign_id) == "FAILED"
        rows = await _audit_rows(pool, campaign_id=campaign_id)
        rollup = [r for r in rows if r["event_type"] == "CAMPAIGN_COMPLETED"]
        assert len(rollup) == 1
        extra = (
            json.loads(rollup[0]["extra"])
            if isinstance(rollup[0]["extra"], str)
            else dict(rollup[0]["extra"])
        )
        assert extra["terminal"] == "FAILED"
        assert extra["failed"] == 2

    async def test_concurrent_last_two_terminal_emits_one_rollup(self, pool: asyncpg.Pool) -> None:
        # Race: two calls go terminal simultaneously. The CAS on
        # campaigns.status='ACTIVE' is the serialization point — only one
        # CAMPAIGN_COMPLETED row lands.
        campaign_id = await _seed_campaign(pool, status="ACTIVE")
        call_a = await _seed_call(
            pool, campaign_id, phone="+14155552100", status="IN_PROGRESS", attempt_epoch=1
        )
        call_b = await _seed_call(
            pool, campaign_id, phone="+14155552101", status="IN_PROGRESS", attempt_epoch=1
        )

        async def finish(call_id: UUID) -> TransitionResult:
            async with pool.acquire() as conn, conn.transaction():
                return await transition(
                    conn,
                    call_id=call_id,
                    expected_status=CallStatus.IN_PROGRESS,
                    new_status=CallStatus.COMPLETED,
                    expected_epoch=1,
                    event_type="TRANSITION",
                    reason="provider completed",
                )

        r1, r2 = await asyncio.gather(finish(call_a), finish(call_b))
        assert r1.applied
        assert r2.applied

        assert await _campaign_status(pool, campaign_id) == "COMPLETED"
        rows = await _audit_rows(pool, campaign_id=campaign_id)
        rollup = [r for r in rows if r["event_type"] == "CAMPAIGN_COMPLETED"]
        assert len(rollup) == 1


class TestQueuedBackToRetryPendingDoesNotRollup:
    async def test_retry_pending_keeps_campaign_active(self, pool: asyncpg.Pool) -> None:
        campaign_id = await _seed_campaign(pool, status="ACTIVE")
        call_id = await _seed_call(
            pool, campaign_id, phone="+14155553001", status="DIALING", attempt_epoch=1
        )

        next_attempt = datetime.now(tz=UTC) + timedelta(seconds=30)
        async with pool.acquire() as conn, conn.transaction():
            await transition(
                conn,
                call_id=call_id,
                expected_status=CallStatus.DIALING,
                new_status=CallStatus.RETRY_PENDING,
                expected_epoch=1,
                event_type="TRANSITION",
                reason="provider_unavailable",
                column_updates={
                    "next_attempt_at": next_attempt,
                    "retries_remaining": 1,
                },
            )

        # RETRY_PENDING is non-terminal — rollup must not fire.
        assert await _campaign_status(pool, campaign_id) == "ACTIVE"
        rows = await _audit_rows(pool, campaign_id=campaign_id)
        rollup = [r for r in rows if r["event_type"] == "CAMPAIGN_COMPLETED"]
        assert rollup == []
